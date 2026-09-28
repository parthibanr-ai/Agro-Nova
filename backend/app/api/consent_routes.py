"""Privacy notice and consent endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.db import get_db
from app.models import User
from app.services import consent

router = APIRouter(prefix="/api/v1")


class ConsentChoices(BaseModel):
    notice_version: str
    purposes: dict[str, bool]


@router.get("/consent/notice")
def get_notice() -> dict:
    """The current privacy notice. Public: a farmer reads it before they have an account."""
    return consent.notice()


@router.get("/me/consent")
def get_consent(user: User = Depends(current_user)) -> dict:
    return consent.state(user)


@router.put("/me/consent")
def put_consent(body: ConsentChoices, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Record the farmer's choices for the current notice version. Sending the same choices again changes nothing,
    so an app that was offline can safely retry."""
    try:
        return consent.record(db, user, body.notice_version, body.purposes)
    except consent.NoticeChanged as e:
        raise HTTPException(409, str(e)) from e
    except consent.ConsentError as e:
        raise HTTPException(422, str(e)) from e
