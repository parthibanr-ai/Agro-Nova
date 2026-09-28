"""A minimal task queue: run named background tasks on a bounded worker pool.

Used to fan a big job out into many small, independent, retry-safe tasks (for example one push-notification digest
per audience) instead of doing everything inside one web request.

Tasks run on this instance's thread pool. To spread them over a fleet, enqueue them on Cloud Tasks (or Pub/Sub)
instead, pointing the queue's HTTP target at `POST /api/v1/internal/tasks/{name}`: that endpoint runs exactly the
same handler. Handlers must therefore be idempotent (a task can be delivered twice); see notifications.run_segment.
"""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from app.core.config import get_settings

logger = logging.getLogger(__name__)

Handler = Callable[[dict], dict]
_HANDLERS: dict[str, Handler] = {}
_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
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


def enqueue(name: str, payload: dict) -> Future | None:
    if name not in _HANDLERS:
        raise KeyError(name)
    if inline:
        _safe(name, payload)
        return None
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
