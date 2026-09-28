"""Endpoints for schedulers and operators (X-API-Key admin key), and the task endpoint that Cloud Tasks calls."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_admin, require_task_caller
from app.db import get_db
from app.services import notifications, tasks

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])
# Separate router: the admin key alone would force Cloud Tasks to carry a long-lived secret, so this one also accepts
# the short-lived OIDC token Cloud Tasks signs for TASK_SERVICE_ACCOUNT.
internal_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_task_caller)])


@router.post("/admin/notifications/dispatch")
def dispatch(dry_run: bool = True, day: date | None = None, db: Session = Depends(get_db)) -> dict:
    """Plan the day's push digests. With `dry_run=false`, queues one task per audience and returns at once; watch
    progress with GET /admin/notifications/runs. Running it twice for the same day sends nothing twice."""
    return notifications.dispatch(db, dry_run=dry_run, run_date=day)


@router.get("/admin/notifications/runs")
def runs(day: date | None = None, db: Session = Depends(get_db)) -> dict:
    return notifications.runs_summary(db, day or date.today())


@internal_router.post("/internal/tasks/{name}")
def run_task(name: str, payload: dict) -> dict:
    """Run a named task now. This is the HTTP target of the Cloud Tasks queues (see services/tasks.py)."""
    try:
        return tasks.run_now(name, payload) or {}
    except KeyError as e:
        raise HTTPException(404, f"Unknown task '{name}'") from e
