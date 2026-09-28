"""A minimal task queue: run named background tasks, on this instance or on Google Cloud Tasks.

Used to fan a big job out into many small, independent, retry-safe tasks (one push-notification digest per audience,
one AI job) instead of doing everything inside one web request.

  TASK_BACKEND=local       tasks run on this instance's bounded thread pool (default; fine for a pilot).
  TASK_BACKEND=cloudtasks  each task becomes a Cloud Tasks HTTP task. Cloud Tasks calls
                           `POST /api/v1/internal/tasks/{name}` on whichever instance has room, signed with an OIDC token
                           for TASK_SERVICE_ACCOUNT, retries failures with backoff, and honours the queue's dispatch
                           rate and concurrency limits. The endpoint runs exactly the same handler as the local pool.

Handlers must be idempotent (a task can be delivered twice); see notifications.run_segment. If Cloud Tasks cannot be
reached when a task is enqueued, that task falls back to the local pool rather than being lost.
"""

import json
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from app.core.config import get_settings

logger = logging.getLogger(__name__)

Handler = Callable[[dict], dict]
_HANDLERS: dict[str, Handler] = {}
_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_cloud_client = None
inline = False  # tests: run tasks immediately in the calling thread


def register(name: str, handler: Handler) -> None:
    _HANDLERS[name] = handler


def run_now(name: str, payload: dict) -> dict:
    """Run a task in this thread (the HTTP-target endpoint, and inline mode)."""
    if name not in _HANDLERS:
        raise KeyError(name)
    return _HANDLERS[name](payload)


def _safe(name: str, payload: dict) -> dict | None:
    try:
        return run_now(name, payload)
    except Exception:  # noqa: BLE001 - one bad task must not stop the others
        logger.exception("task %s failed", name)
        return None


# ------------------------------------------------------------------ Cloud Tasks
def _client():
    global _cloud_client
    with _lock:
        if _cloud_client is None:
            from google.cloud import tasks_v2

            _cloud_client = tasks_v2.CloudTasksClient()
        return _cloud_client


def _project() -> str:
    s = get_settings()
    return s.cloud_tasks_project or os.environ.get("GOOGLE_CLOUD_PROJECT") or s.gee_cloud_project or ""


def enqueue_cloud(queue: str, name: str, payload: dict, dedupe_id: str | None = None) -> None:
    """Create a Cloud Tasks HTTP task that will POST `payload` to /api/v1/internal/tasks/{name}.

    `dedupe_id` (letters, digits, - and _) becomes the task name, so enqueueing the same id twice is a no-op. Only
    use it for work that is never meant to be re-enqueued later (Cloud Tasks remembers a name for about an hour).
    Raises if the task cannot be created.
    """
    from google.api_core.exceptions import AlreadyExists

    s = get_settings()
    client = _client()
    parent = client.queue_path(_project(), s.cloud_tasks_location or s.gcp_location, queue)
    task: dict = {
        "http_request": {
            "http_method": "POST",
            "url": f"{s.task_target_url.rstrip('/')}/api/v1/internal/tasks/{name}",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            "oidc_token": {"service_account_email": s.task_service_account,
                           "audience": s.task_audience or s.task_target_url},
        }
    }
    if dedupe_id:
        task["name"] = client.task_path(_project(), s.cloud_tasks_location or s.gcp_location, queue, dedupe_id)
    try:
        client.create_task(request={"parent": parent, "task": task})
    except AlreadyExists:
        pass  # the same task is already queued: that is what dedupe_id is for


# ------------------------------------------------------------------ public API
def enqueue(name: str, payload: dict) -> Future | None:
    if name not in _HANDLERS:
        raise KeyError(name)
    if inline:
        _safe(name, payload)
        return None
    if get_settings().task_backend == "cloudtasks":
        try:
            enqueue_cloud(get_settings().cloud_tasks_queue, name, payload)
            return None
        except Exception:  # noqa: BLE001
            logger.exception("could not queue task %s on Cloud Tasks; running it on this instance instead", name)
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=max(1, get_settings().task_workers),
                                           thread_name_prefix="task")
        ex = _executor
    return ex.submit(_safe, name, payload)


def shutdown(wait: bool = True) -> None:
    global _executor
    with _lock:
        ex, _executor = _executor, None
    if ex is not None:
        ex.shutdown(wait=wait, cancel_futures=True)
