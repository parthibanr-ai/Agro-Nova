"""Push notifications via Firebase Cloud Messaging.

`build_digest` decides what each farmer should hear about (new schemes for their profile, top weather/ENSO
advisory, a value-addition/market nudge); `send_push` delivers it. Content is localised by the same
translation layer as API responses. Without Firebase credentials it runs as a dry run and only logs.
"""

import logging

from sqlalchemy.orm import Session

from app.core.languages import get_language
from app.models import Plot, User
from app.services import advice, knowledge
from app.services.enso import get_enso_state
from app.services.forecast import enso_advisory
from app.services.translation import localize_payload

logger = logging.getLogger(__name__)


def build_digest(user: User, plot: Plot | None, seen_scheme_ids: set[str] | None = None) -> list[dict]:
    seen = seen_scheme_ids or set()
    area_ha = plot.area_m2 / 10_000 if plot else None
    crop = plot.crop if plot else None
    msgs: list[dict] = []

    for s in advice.matching_schemes(user.country, user.state, crop, area_ha):
        if s["id"] not in seen:
            msgs.append({"topic": "scheme", "title": f"Scheme: {s['name']}", "body": s["summary"], "url": s["url"], "ref": s["id"]})

    if plot:
        adv = enso_advisory(get_enso_state(), plot.country, plot.crop)
        if adv:
            msgs.append({"topic": "weather", "title": adv["title"], "body": adv["detail"], "ref": "enso"})
        va = knowledge.load("value_add")["crops"].get(plot.crop)
        if va:
            msgs.append({"topic": "market", "title": f"Earn more from your {knowledge.crop(plot.crop)['name']}", "body": va[0], "ref": f"va_{plot.crop}"})
    return msgs


def send_push(token: str, title: str, body: str, data: dict | None = None) -> bool:
    try:
        from firebase_admin import messaging

        from app.core.auth import ensure_firebase

        ensure_firebase()
    except Exception:  # noqa: BLE001
        logger.info("[no Firebase credentials - not sent] %s | %s", title, body)
        return False
    try:
        messaging.send(messaging.Message(token=token, notification=messaging.Notification(title=title, body=body), data=data or {}))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("FCM send failed: %s", exc)
        return False


def dispatch_all(db: Session, dry_run: bool = True) -> list[dict]:
    """Build (and unless dry_run, send) digests for every user with a registered device token."""
    report = []
    for user in db.query(User).filter(User.fcm_token.isnot(None)).all():
        plot = db.query(Plot).filter(Plot.owner_uid == user.uid).first()
        msgs = localize_payload(build_digest(user, plot), get_language(user.language).code)
        sent = 0
        for m in msgs:
            if not dry_run and send_push(user.fcm_token, m["title"], m["body"], {"topic": m["topic"], "ref": m["ref"]}):
                sent += 1
        report.append({"uid": user.uid, "language": user.language, "messages": msgs, "sent": sent})
    return report
