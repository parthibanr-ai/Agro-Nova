"""Privacy notice and the farmer's consent choices (India's DPDP Act: notice, purpose-specific consent, withdrawal as
easy as giving it, and a record of both).

The notice text lives in app/data/privacy_notice.json with a version. Changing the wording in a way that matters
means bumping the version, which makes every farmer's stored consent stale so the app asks again. A farmer's choices
are kept on their user row (current state) and in consent_events (history).
"""

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ConsentEvent, User

_NOTICE = Path(__file__).resolve().parents[1] / "data" / "privacy_notice.json"


class ConsentError(ValueError):
    """The submitted choices cannot be recorded (unknown purpose, required purpose refused)."""


class NoticeChanged(ConsentError):
    """The farmer answered an older version of the notice."""


@lru_cache
def _raw() -> dict:
    return json.loads(_NOTICE.read_text(encoding="utf-8"))


def version() -> str:
    return _raw()["version"]


def purposes() -> list[dict]:
    return _raw()["purposes"]


def required_purposes() -> list[str]:
    return [p["id"] for p in purposes() if p["required"]]


def notice() -> dict:
    """The notice as the app shows it, with this deployment's retention period and contacts filled in."""
    s = get_settings()
    raw = json.loads(json.dumps(_raw()))  # a copy, so the cached original is never edited
    for sec in raw["sections"]:
        sec["body"] = sec["body"].replace("{retention_days}", str(s.retention_days))
    raw["controller"] = {"name": s.data_controller_name}
    raw["grievance_officer"] = {"name": s.grievance_officer_name, "email": s.grievance_officer_email}
    return raw


def has(user: User, purpose: str) -> bool:
    """True if the farmer agreed to `purpose` under the current notice."""
    return user.consent_version == version() and bool((user.consents or {}).get(purpose))


def state(user: User) -> dict:
    granted = {p["id"]: has(user, p["id"]) for p in purposes()}
    return {
        "notice_version": version(),
        "accepted_version": user.consent_version,
        "purposes": granted,
        # Old acceptance, or a refused required purpose: the app must show the notice again.
        "needs_consent": not all(granted[p] for p in required_purposes()),
        "enforcement": get_settings().consent_mode,
        "consented_at": user.consented_at.isoformat() if user.consented_at else None,
    }


def record(db: Session, user: User, notice_version: str, choices: dict[str, bool]) -> dict:
    """Store the farmer's choices. Every purpose must be answered; every change is logged as an event."""
    if notice_version != version():
        raise NoticeChanged(f"The privacy notice has been updated (now {version()}). Please read it again.")
    known = {p["id"] for p in purposes()}
    unknown = set(choices) - known
    if unknown:
        raise ConsentError(f"Unknown purpose: {', '.join(sorted(unknown))}")
    missing = known - set(choices)
    if missing:
        raise ConsentError(f"Please answer every choice (missing: {', '.join(sorted(missing))}).")
    for pid in required_purposes():
        if not choices[pid]:
            raise ConsentError("The plots and farm details consent is needed to use the app. To stop, delete your "
                               "data from the menu instead.")

    before = user.consents if user.consent_version == version() else {}
    now = datetime.now(timezone.utc)
    for pid, granted in choices.items():
        if bool(before.get(pid)) != bool(granted) or pid not in before:
            db.add(ConsentEvent(uid=user.uid, purpose=pid, granted=bool(granted), notice_version=version(),
                                created_at=now))
    user.consents = {pid: bool(v) for pid, v in choices.items()}
    user.consent_version = version()
    user.consented_at = now
    if not user.consents.get("notifications"):
        user.fcm_token = None  # withdrawing means we stop holding the device token, not just stop using it
    db.merge(user)
    db.commit()
    return state(user)


def history(db: Session, uid: str) -> list[dict]:
    rows = db.query(ConsentEvent).filter(ConsentEvent.uid == uid).order_by(ConsentEvent.id).all()
    return [{"purpose": r.purpose, "granted": r.granted, "notice_version": r.notice_version,
             "at": r.created_at.isoformat()} for r in rows]
