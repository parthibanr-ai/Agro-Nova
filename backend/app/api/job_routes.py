"""Job-style endpoints for the slow requests: crop recommendation, photo diagnosis, the other AI screens, forecast.

    POST /plots/{id}/crop-recommendation/jobs      -> 200 with the answer if it is ready within ~2 s (cached), else
    POST /diagnosis/jobs   (multipart, like /diagnosis)   202 with a job id and a Location header to poll
    POST /resilience/jobs, /water-tips/jobs        (?plot_id=)   the same for the resilience and water screens
    POST /plots/{id}/market/jobs, /forecast/jobs   the same for value addition / market and the forecast (?days=)
    GET  /jobs/{id}                                -> {"status": "queued|running|done|failed", "result"/"error"}

The original synchronous endpoints keep working for older app versions. The job handlers call the very same
route functions, so both paths always behave identically.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from app.api import routes
from app.core.auth import current_user, require_admin
from app.core.languages import authoring_language
from app.core.ratelimit import limit
from app.db import get_db
from app.models import User
from app.services import jobs

router = APIRouter(prefix="/api/v1")

RETRY_AFTER_S = 2


# ------------------------------------------------------------------ what a worker runs
def _crop_recommendation(db: Session, user: User, params: dict, payload) -> dict:
    return routes.crop_recommendation_for_plot(params["plot_id"], lang=params.get("lang"), user=user, db=db)


def _diagnosis(db: Session, user: User, params: dict, payload) -> dict:
    return routes.run_diagnosis(db, user, payload, params["content_type"], params.get("crop"), params.get("notes"),
                                params.get("plot_id"))


def _resilience(db: Session, user: User, params: dict, payload) -> dict:
    return routes.resilience(params.get("plot_id"), lang=params.get("lang"), user=user, db=db)


def _water_tips(db: Session, user: User, params: dict, payload) -> dict:
    return routes.water_tips(params.get("plot_id"), lang=params.get("lang"), user=user, db=db)


def _market(db: Session, user: User, params: dict, payload) -> dict:
    return routes.market(params["plot_id"], lang=params.get("lang"), user=user, db=db)


def _forecast(db: Session, user: User, params: dict, payload) -> dict:
    return routes.forecast(params["plot_id"], days=params.get("days", 10), user=user, db=db)


jobs.register("crop_recommendation", _crop_recommendation)
jobs.register("diagnosis", _diagnosis)
jobs.register("resilience", _resilience)
jobs.register("water_tips", _water_tips)
jobs.register("market", _market)
jobs.register("forecast", _forecast)


# ------------------------------------------------------------------ helpers
def _authoring_code(lang: str | None) -> str | None:
    """The language Gemini writes this job's advice in (part of the job, so a Hindi and an English request differ)."""
    language = authoring_language(lang)
    return language.code if language else None


def _start(db: Session, user: User, kind: str, params: dict, response: Response, payload=None) -> dict:
    try:
        job = jobs.enqueue(db, user, kind, params, payload)
    except jobs.QueueFull as e:
        raise HTTPException(503, "The service is very busy. Please try again in a few seconds.",
                            headers={"Retry-After": "5"}) from e
    if job.status == "done":
        return jobs.view(job)
    if job.status == "failed":
        # Failed within the quick wait: answer exactly as the synchronous endpoint would have.
        raise HTTPException(job.error["status"], job.error["message"])
    response.status_code = 202
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    response.headers["Retry-After"] = str(RETRY_AFTER_S)
    return jobs.view(job)


# ------------------------------------------------------------------ endpoints
@router.post("/plots/{plot_id}/crop-recommendation/jobs", dependencies=[Depends(limit("ai"))])
def start_crop_recommendation(plot_id: str, response: Response, notify: bool = False, lang: str | None = None,
                              user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    routes._plot_or_404(db, user, plot_id)  # a wrong plot id is an immediate 404, not a failed job
    return _start(db, user, "crop_recommendation",
                  {"plot_id": plot_id, "notify": notify, "lang": _authoring_code(lang)}, response)


@router.post("/diagnosis/jobs", dependencies=[Depends(limit("diagnosis"))])
def start_diagnosis(
    response: Response,
    image: UploadFile = File(...),
    crop: str | None = Form(None),
    notes: str | None = Form(None),
    plot_id: str | None = Form(None),
    notify: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    data = routes.read_photo(image)
    if plot_id:
        routes._plot_or_404(db, user, plot_id)
    params = {"content_type": image.content_type, "crop": crop, "notes": notes, "plot_id": plot_id, "notify": notify}
    return _start(db, user, "diagnosis", params, response, payload=data)


@router.post("/resilience/jobs", dependencies=[Depends(limit("ai"))])
def start_resilience(response: Response, plot_id: str | None = None, notify: bool = False, lang: str | None = None,
                     user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if plot_id:
        routes._plot_or_404(db, user, plot_id)
    return _start(db, user, "resilience", {"plot_id": plot_id, "notify": notify, "lang": _authoring_code(lang)},
                  response)


@router.post("/water-tips/jobs", dependencies=[Depends(limit("ai"))])
def start_water_tips(response: Response, plot_id: str | None = None, notify: bool = False, lang: str | None = None,
                     user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if plot_id:
        routes._plot_or_404(db, user, plot_id)
    return _start(db, user, "water_tips", {"plot_id": plot_id, "notify": notify, "lang": _authoring_code(lang)},
                  response)


@router.post("/plots/{plot_id}/market/jobs", dependencies=[Depends(limit("ai"))])
def start_market(plot_id: str, response: Response, notify: bool = False, lang: str | None = None,
                 user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    routes._plot_or_404(db, user, plot_id)
    return _start(db, user, "market", {"plot_id": plot_id, "notify": notify, "lang": _authoring_code(lang)}, response)


@router.post("/plots/{plot_id}/forecast/jobs")
def start_forecast(plot_id: str, response: Response, days: int = 10, notify: bool = False,
                   user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    routes._plot_or_404(db, user, plot_id)
    return _start(db, user, "forecast", {"plot_id": plot_id, "days": min(max(days, 1), 16), "notify": notify}, response)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = jobs.get_for_owner(db, user, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return jobs.view(job)


@router.post("/jobs/{job_id}/notify")
def notify_when_ready(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Called when the app goes to the background with a job still running: push "your answer is ready"."""
    outcome = jobs.request_notification(db, user, job_id)
    if outcome is None:
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "status": outcome}


@router.post("/admin/jobs/purge", dependencies=[Depends(require_admin)])
def purge_jobs(db: Session = Depends(get_db)) -> dict:
    """For the scheduler: delete expired jobs now (they are also purged opportunistically)."""
    return {"purged": jobs.purge_expired(db, force=True)}
