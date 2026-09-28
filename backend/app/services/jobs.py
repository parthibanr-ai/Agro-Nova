"""Jobs: accept a slow AI request now, finish it on a worker, and let the client poll for the answer.

Why: a crop recommendation or photo diagnosis can take from a second to over half a minute (Earth Engine, then
Gemini, with retries). Held inside a normal request that pins a server thread and a database connection for the
whole time, and a phone on a weak connection has to keep the socket open. As a job the request returns at once
(or within a couple of seconds if the answer is already cached) and the phone polls a cheap status endpoint.

The database row is the shared state, so any instance can answer a poll. The work itself runs on the instance
that accepted it, on a bounded thread pool; if that instance dies, the job is marked failed after a lease
(JOB_LEASE_S) and the app simply retries. Photo bytes are held in memory only, never stored.

To move execution to a queue (Cloud Tasks / Pub/Sub) later, replace `_get_executor().submit(_run, ...)` with an
enqueue call whose worker invokes `_run(job_id, payload)`; the claiming step below is already safe against a
task being delivered twice.
"""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import Job, User

logger = logging.getLogger(__name__)

Handler = Callable[[Session, User, dict, Any], dict]
_HANDLERS: dict[str, Handler] = {}
ACTIVE = ("queued", "running")
_PURGE_EVERY_S = 60.0

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_pending = 0  # accepted but unfinished jobs on this instance
_futures: dict[str, Future] = {}
_last_purge = 0.0


class QueueFull(RuntimeError):
    """This instance already has JOB_QUEUE_MAX unfinished jobs."""


def register(kind: str, handler: Handler) -> None:
    _HANDLERS[kind] = handler


def _now() -> datetime:
    return datetime.now(timezone.utc)


def queue_depth() -> int:
    return _pending


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=max(1, get_settings().job_workers), thread_name_prefix="job")
        return _executor


def shutdown() -> None:
    """Stop accepting work and drop queued jobs (application shutdown, and between tests)."""
    global _executor, _pending
    with _lock:
        ex, _executor = _executor, None
    if ex is not None:
        ex.shutdown(wait=True, cancel_futures=True)
    with _lock:
        _pending = 0
        _futures.clear()


def _fingerprint(uid: str, kind: str, params: dict, payload: Any) -> str:
    digest = hashlib.sha256(payload).hexdigest() if isinstance(payload, (bytes, bytearray)) else None
    return hashlib.sha256(json.dumps([uid, kind, params, digest], sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ accepting a job
def enqueue(db: Session, user: User, kind: str, params: dict, payload: Any = None,
            wait_s: float | None = None) -> Job:
    """Create the job (or return the identical one still running), start it, and wait briefly for a quick answer.

    Sending the same request again while it is running, for example a double tap or a retry on a flaky network,
    returns the job already in progress instead of doing the work twice.
    """
    global _pending
    s = get_settings()
    if kind not in _HANDLERS:
        raise ValueError(f"Unknown job kind '{kind}'")
    purge_expired(db)
    fingerprint = _fingerprint(user.uid, kind, params, payload)
    job = db.scalars(
        select(Job).where(Job.owner_uid == user.uid, Job.fingerprint == fingerprint, Job.status.in_(ACTIVE),
                          Job.expires_at > _now()).order_by(Job.created_at.desc()).limit(1)
    ).first()
    future: Future | None = None
    if job is None:
        with _lock:
            if _pending >= s.job_queue_max:
                raise QueueFull()
            _pending += 1
        try:
            now = _now()
            job = Job(owner_uid=user.uid, kind=kind, fingerprint=fingerprint, params=params, status="queued",
                      created_at=now, expires_at=now + timedelta(seconds=s.job_ttl_s))
            db.add(job)
            db.commit()
            future = _get_executor().submit(_run, job.id, payload)
        except Exception:
            with _lock:
                _pending -= 1
            raise
        with _lock:
            _futures[job.id] = future
        future.add_done_callback(lambda _f, jid=job.id: _finished(jid))
    else:
        future = _futures.get(job.id)  # only present if this instance is the one running it

    wait = s.job_fast_wait_s if wait_s is None else wait_s
    if future is not None and wait > 0:
        try:
            future.result(timeout=wait)
        except FutureTimeout:
            pass
        except Exception:  # noqa: BLE001 - failures are recorded on the job row by the worker
            pass
    db.commit()  # end any read transaction so refresh() sees the worker's committed result
    db.refresh(job)
    return job


def _finished(job_id: str) -> None:
    global _pending
    with _lock:
        _futures.pop(job_id, None)
        _pending = max(0, _pending - 1)


# ------------------------------------------------------------------ running a job
def _failure(status: int, message: str) -> dict:
    return {"status": "failed", "result": None, "error": {"status": status, "message": message}}


def _run(job_id: str, payload: Any) -> None:
    with SessionLocal() as db:
        # Claim it atomically: if a duplicate delivery or another instance got there first, do nothing.
        claimed = db.execute(update(Job).where(Job.id == job_id, Job.status == "queued")
                             .values(status="running", started_at=_now())
                             .execution_options(synchronize_session=False)).rowcount
        db.commit()
        if claimed != 1:
            return
        job = db.get(Job, job_id)
        user = db.get(User, job.owner_uid)
        kind, params = job.kind, dict(job.params or {})
        try:
            outcome = {"status": "done", "result": jsonable_encoder(_HANDLERS[kind](db, user, params, payload)),
                       "error": None}
        except HTTPException as e:
            outcome = _failure(e.status_code, str(e.detail))
        except Exception:  # noqa: BLE001
            logger.exception("job %s (%s) crashed", job_id, kind)
            outcome = _failure(500, "Something went wrong. Please try again.")
        db.rollback()  # the handler may have left a transaction open
        db.execute(update(Job).where(Job.id == job_id, Job.status == "running")
                   .values(finished_at=_now(), **outcome).execution_options(synchronize_session=False))
        db.commit()
        if params.get("notify") and outcome["status"] == "done" and user.fcm_token:
            _notify(user, job_id, kind)


def _notify(user: User, job_id: str, kind: str) -> None:
    """Tell the farmer their answer is ready, so they need not keep the screen open."""
    from app.core.languages import get_language
    from app.services import notifications
    from app.services.translation import localize_payload

    title = {"crop_recommendation": "Your crop recommendation is ready", "diagnosis": "Your plant diagnosis is ready"}[kind]
    try:
        msg = localize_payload({"title": title, "body": "Open Agro Nova to see it."}, get_language(user.language).code)
        notifications.send_push(user.fcm_token, msg["title"], msg["body"], {"job_id": job_id, "kind": kind})
    except Exception:  # noqa: BLE001 - a failed push must never fail the job
        logger.warning("Could not push job %s completion", job_id, exc_info=True)


# ------------------------------------------------------------------ polling
def view(job: Job) -> dict:
    out: dict = {"job_id": job.id, "kind": job.kind, "status": job.status}
    if job.status == "done":
        out["result"] = job.result
    elif job.status == "failed":
        out["error"] = job.error
    else:
        out["retry_after_s"] = 2
    return out


def get_for_owner(db: Session, user: User, job_id: str) -> Job | None:
    """The caller's job, or None. A job unfinished past its lease is presumed lost and marked failed."""
    job = db.get(Job, job_id)
    if job is None or job.owner_uid != user.uid:
        return None
    if job.status in ACTIVE:
        cutoff = _now() - timedelta(seconds=get_settings().job_lease_s)
        if db.execute(update(Job).where(Job.id == job_id, Job.status.in_(ACTIVE), Job.created_at < cutoff)
                      .values(status="failed", finished_at=_now(),
                              error={"status": 503, "message": "This took too long and was interrupted. "
                                                               "Please try again."})
                      .execution_options(synchronize_session=False)).rowcount:
            db.commit()
            db.refresh(job)
    return job


def purge_expired(db: Session, force: bool = False, limit: int = 500) -> int:
    """Delete expired jobs. Runs at most once a minute per instance unless forced (admin/scheduler)."""
    global _last_purge
    now = time.monotonic()
    if not force and now - _last_purge < _PURGE_EVERY_S:
        return 0
    _last_purge = now
    ids = db.scalars(select(Job.id).where(Job.expires_at < _now()).limit(limit)).all()
    if not ids:
        db.rollback()
        return 0
    db.execute(delete(Job).where(Job.id.in_(ids)).execution_options(synchronize_session=False))
    db.commit()
    return len(ids)
