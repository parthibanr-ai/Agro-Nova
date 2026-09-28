"""A farmer's right to see and erase their data (India's DPDP Act, and good practice anywhere).

Erasure removes the profile (including the device push token and location-bearing plots), soil samples, the plots'
satellite snapshots and every AI job row. Advice caches hold no personal data: they are keyed by state, crop and rounded conditions, never by farmer.
The Firebase sign-in account is deleted too, so the same identity cannot silently come back with a profile.
"""

import hashlib
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.models import Job, Plot, User
from app.services import climate_snapshots

logger = logging.getLogger(__name__)


def export_data(db: Session, user: User) -> dict:
    """Everything stored about the farmer, as plain JSON (the right to access)."""
    plots = db.query(Plot).filter(Plot.owner_uid == user.uid).order_by(Plot.created_at).all()
    return {
        "profile": {
            "uid": user.uid, "language": user.language, "country": user.country, "state": user.state,
            "livestock": user.livestock, "has_push_token": bool(user.fcm_token),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "plots": [
            {"id": p.id, "name": p.name, "country": p.country, "state": p.state, "district": p.district,
             "crop": p.crop, "sowing_date": p.sowing_date.isoformat() if p.sowing_date else None,
             "corners": p.corners, "area_m2": p.area_m2,
             "soil_samples": [{"id": s.id, "lat": s.lat, "lon": s.lon, "source": s.source,
                               "sampled_on": s.sampled_on.isoformat() if s.sampled_on else None, "values": s.values}
                              for s in p.soil_samples]}
            for p in plots],
    }


def erase(db: Session, user: User) -> None:
    """Delete the farmer's data. The row goes first, so a failure abroad (Firebase) never leaves data behind."""
    uid = user.uid
    climate_snapshots.erase_for_plots(db, list(db.scalars(select(Plot.id).where(Plot.owner_uid == uid))))
    db.execute(delete(Job).where(Job.owner_uid == uid).execution_options(synchronize_session=False))
    db.delete(user)  # cascades to plots and their soil samples
    db.commit()
    metrics.ACCOUNTS_DELETED.inc()
    # The log keeps proof that the request was honoured without keeping the identity it was about.
    logger.info("account erased (id hash %s)", hashlib.sha256(uid.encode()).hexdigest()[:12])
    if get_settings().auth_mode == "firebase":
        _delete_firebase_account(uid)


def _delete_firebase_account(uid: str) -> None:
    try:
        from firebase_admin import auth

        from app.core.auth import ensure_firebase

        ensure_firebase()
        auth.delete_user(uid)
    except Exception:  # noqa: BLE001 - the data is already gone; an orphaned sign-in holds no farm data
        logger.warning("could not delete the Firebase account after erasing the data", exc_info=True)
