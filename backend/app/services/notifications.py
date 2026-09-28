"""Push notifications via Firebase Cloud Messaging, computed once per audience and sent in batches.

The old job loaded every farmer, ran a query and a translation per farmer, and sent one message at a time inside a
single web request; it could not finish for more than a few thousand farmers. Now:

  1. Farmers are grouped into audiences ("segments"): country, state, the crop of their first plot, and language.
     What a farmer should hear depends only on those four things, so the digest (matching schemes, the weather/ENSO
     advisory, a market nudge) is built and translated ONCE per audience, not once per farmer.
  2. Dispatch queues one small task per audience (see tasks.py; a fleet can use Cloud Tasks instead).
  3. Each task walks that audience's device tokens in pages of 500 (keyset pagination, constant memory) and sends
     each message with FCM's multicast call, 500 devices per API call.
  4. Every audience-day is claimed in `notification_runs` before sending, so a retried or duplicated task never
     sends twice, and a crashed one is picked up again.
  5. Device tokens that FCM reports as dead are cleared, so tomorrow's run does not pay for them.

Without Firebase credentials nothing is sent (dry run: it only logs).
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.languages import get_language
from app.db import SessionLocal
from app.models import NotificationRun, User
from app.services import advice, knowledge, tasks
from app.services.enso import get_enso_state
from app.services.forecast import enso_advisory
from app.services.translation import localize_payload

logger = logging.getLogger(__name__)

PAGE = 500  # FCM's multicast limit is 500 tokens per call
STALE_RUN_S = 1800  # a run still "running" after this long is presumed crashed and may be taken over


# ------------------------------------------------------------------ audiences
@dataclass(frozen=True)
class Segment:
    country: str
    state: str | None
    crop: str | None
    language: str

    @property
    def key(self) -> str:
        return f"{self.country}|{self.state or ''}|{self.crop or ''}|{self.language}"

    @classmethod
    def from_key(cls, key: str) -> "Segment":
        country, state, crop, language = key.split("|")
        return cls(country, state or None, crop or None, language)


def list_segments(db: Session) -> list[tuple[Segment, int]]:
    """Each audience with its farmer count, biggest first. One grouped query, however many farmers there are."""
    rows = db.execute(
        select(User.country, User.state, User.primary_crop.label("crop"), User.language, func.count().label("n"))
        .where(User.fcm_token.is_not(None))
        .group_by(User.country, User.state, User.primary_crop, User.language).order_by(func.count().desc())
    ).all()
    return [(Segment(r.country, r.state, r.crop, r.language), r.n) for r in rows]


def members_page(db: Session, seg: Segment, after_uid: str, limit: int = PAGE) -> list[tuple[str, str]]:
    """The next (uid, device token) pairs of an audience after `after_uid`, in uid order."""
    q = select(User.uid, User.fcm_token).where(User.fcm_token.is_not(None), User.country == seg.country,
                                               User.language == seg.language, User.uid > after_uid)
    q = q.where(User.state.is_(None) if seg.state is None else User.state == seg.state)
    q = q.where(User.primary_crop.is_(None) if seg.crop is None else User.primary_crop == seg.crop)
    return [(r.uid, r.fcm_token) for r in db.execute(q.order_by(User.uid).limit(limit)).all()]


# ------------------------------------------------------------------ content
def build_digest(country: str, state: str | None, crop: str | None, seen_scheme_ids: set[str] | None = None,
                 max_new_schemes: int | None = None) -> list[dict]:
    """What an audience should hear about today (English; localised by `digest_for`).

    Schemes already announced to this audience are skipped, and at most `max_new_schemes` new ones go out per day.
    """
    seen = seen_scheme_ids or set()
    cap = get_settings().notify_max_new_schemes_per_day if max_new_schemes is None else max_new_schemes
    msgs: list[dict] = []
    for s in [s for s in advice.matching_schemes(country, state, crop, None) if s["id"] not in seen][:cap]:
        msgs.append({"topic": "scheme", "title": f"Scheme: {s['name']}", "body": s["summary"], "url": s["url"],
                     "ref": s["id"]})
    if crop:
        adv = enso_advisory(get_enso_state(), country, crop)
        if adv:
            msgs.append({"topic": "weather", "title": adv["title"], "body": adv["detail"], "ref": "enso"})
        va = knowledge.load("value_add")["crops"].get(crop)
        if va:
            msgs.append({"topic": "market", "title": f"Earn more from your {knowledge.crop(crop)['name']}",
                         "body": va[0], "ref": f"va_{crop}"})
    return msgs


def announced_schemes(db: Session, key: str, keep_runs: int = 90) -> set[str]:
    """Scheme ids this audience has already been told about (from its recent finished runs)."""
    rows = db.scalars(select(NotificationRun.refs).where(NotificationRun.segment_key == key,
                                                         NotificationRun.status == "done")
                      .order_by(NotificationRun.run_date.desc()).limit(keep_runs)).all()
    return {ref for refs in rows for ref in (refs or [])}


def digest_for(seg: Segment, seen_scheme_ids: set[str] | None = None) -> list[dict]:
    return localize_payload(build_digest(seg.country, seg.state, seg.crop, seen_scheme_ids),
                            get_language(seg.language).code)


# ------------------------------------------------------------------ sending
@dataclass
class SendOutcome:
    sent: int = 0
    failed: int = 0
    dead_tokens: list[str] = field(default_factory=list)


def _firebase_messaging():
    from firebase_admin import messaging

    from app.core.auth import ensure_firebase

    ensure_firebase()
    return messaging


def send_push(token: str, title: str, body: str, data: dict | None = None) -> bool:
    """Send to one device (used for "your answer is ready")."""
    try:
        messaging = _firebase_messaging()
    except Exception:  # noqa: BLE001
        logger.info("[no Firebase credentials - not sent] %s | %s", title, body)
        return False
    try:
        messaging.send(messaging.Message(token=token, notification=messaging.Notification(title=title, body=body),
                                         data=data or {}))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("FCM send failed: %s", exc)
        return False


def send_multicast(tokens: list[str], title: str, body: str, data: dict | None = None) -> SendOutcome:
    """Send one message to up to 500 devices in a single FCM call and report which tokens are dead."""
    try:
        messaging = _firebase_messaging()
    except Exception:  # noqa: BLE001
        logger.info("[no Firebase credentials - %d devices not sent] %s", len(tokens), title)
        return SendOutcome()
    resp = messaging.send_each_for_multicast(messaging.MulticastMessage(
        tokens=tokens, notification=messaging.Notification(title=title, body=body), data=data or {}))
    out = SendOutcome()
    dead = (messaging.UnregisteredError, messaging.SenderIdMismatchError)
    for token, r in zip(tokens, resp.responses, strict=True):
        if r.success:
            out.sent += 1
        else:
            out.failed += 1
            if isinstance(r.exception, dead):
                out.dead_tokens.append(token)
    return out


# ------------------------------------------------------------------ one audience, one day
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim(db: Session, run_date: date, key: str) -> NotificationRun | None:
    """Take the (day, audience) lock. Returns None if it is finished or being handled by someone else."""
    run = NotificationRun(run_date=run_date, segment_key=key, status="running", started_at=_now())
    db.add(run)
    try:
        db.commit()
        return run
    except IntegrityError:
        db.rollback()
    stale = _now() - timedelta(seconds=STALE_RUN_S)
    taken = db.execute(
        update(NotificationRun)
        .where(NotificationRun.run_date == run_date, NotificationRun.segment_key == key,
               (NotificationRun.status == "failed")
               | ((NotificationRun.status == "running") & (NotificationRun.started_at < stale)))
        .values(status="running", started_at=_now(), error=None).execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if not taken:
        return None
    return db.scalars(select(NotificationRun).where(NotificationRun.run_date == run_date,
                                                    NotificationRun.segment_key == key)).one()


def run_segment(payload: dict) -> dict:
    """Send today's digest to one audience. Safe to run twice: the second attempt finds the claim and stops."""
    run_date, key = date.fromisoformat(payload["run_date"]), payload["segment"]
    seg = Segment.from_key(key)
    with SessionLocal() as db:
        run = _claim(db, run_date, key)
        if run is None:
            return {"segment": key, "skipped": True}
        users = sent = failed = dead = 0
        messages: list[dict] = []
        try:
            messages = digest_for(seg, announced_schemes(db, key))
            db.rollback()
            after = ""
            while page := members_page(db, seg, after):
                db.rollback()  # free the connection while talking to FCM
                after = page[-1][0]
                tokens = [t for _, t in page]
                users += len(tokens)
                dead_tokens: list[str] = []
                for m in messages:
                    out = send_multicast(tokens, m["title"], m["body"], {"topic": m["topic"], "ref": m["ref"]})
                    sent, failed = sent + out.sent, failed + out.failed
                    dead_tokens += out.dead_tokens
                if dead_tokens:
                    dead_tokens = list(set(dead_tokens))
                    db.execute(update(User).where(User.fcm_token.in_(dead_tokens)).values(fcm_token=None)
                               .execution_options(synchronize_session=False))
                    db.commit()
                    dead += len(dead_tokens)
            status, error = "done", None
        except Exception as exc:  # noqa: BLE001
            logger.exception("notification run %s %s failed", run_date, key)
            db.rollback()
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        db.execute(update(NotificationRun).where(NotificationRun.id == run.id)
                   .values(status=status, users=users, sent=sent, failed=failed, invalid_tokens=dead,
                           refs=[m["ref"] for m in messages if m["topic"] == "scheme"] if status == "done" else [],
                           finished_at=_now(), error=error).execution_options(synchronize_session=False))
        db.commit()
    return {"segment": key, "status": status, "users": users, "sent": sent, "failed": failed, "dead_tokens": dead}


tasks.register("notify-segment", run_segment)


# ------------------------------------------------------------------ dispatch (called by the scheduler)
def dispatch(db: Session, dry_run: bool = True, run_date: date | None = None, report_limit: int = 50) -> dict:
    """Plan today's audiences and, unless `dry_run`, queue one task per audience. Returns immediately."""
    run_date = run_date or date.today()
    segments = list_segments(db)
    report = {"date": run_date.isoformat(), "dry_run": dry_run, "segments": len(segments),
              "users": sum(n for _, n in segments)}
    if dry_run:
        report["digests"] = [
            {"segment": seg.key, "language": seg.language, "users": n,
             "messages": digest_for(seg, announced_schemes(db, seg.key)), "sent": 0}
            for seg, n in segments[:report_limit]
        ]
        return report
    for seg, _ in segments:
        tasks.enqueue("notify-segment", {"run_date": run_date.isoformat(), "segment": seg.key})
    report["queued"] = len(segments)
    return report


def runs_summary(db: Session, run_date: date) -> dict:
    """Progress of a day's dispatch: how many audiences are done, running or failed, and the totals."""
    rows = db.execute(
        select(NotificationRun.status, func.count(), func.coalesce(func.sum(NotificationRun.users), 0),
               func.coalesce(func.sum(NotificationRun.sent), 0), func.coalesce(func.sum(NotificationRun.failed), 0),
               func.coalesce(func.sum(NotificationRun.invalid_tokens), 0))
        .where(NotificationRun.run_date == run_date).group_by(NotificationRun.status)
    ).all()
    out = {"date": run_date.isoformat(), "segments": {}, "users": 0, "sent": 0, "failed": 0, "dead_tokens_cleared": 0}
    for status, n, users, sent, failed, dead in rows:
        out["segments"][status] = n
        out["users"] += users
        out["sent"] += sent
        out["failed"] += failed
        out["dead_tokens_cleared"] += dead
    return out
