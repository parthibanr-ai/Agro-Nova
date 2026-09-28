"""Erase the accounts of farmers who have not used the app for RETENTION_DAYS (the privacy notice promises this).

    python -m app.batch.retention              # shows who would be erased, changes nothing
    python -m app.batch.retention --apply      # erases them
    python -m app.batch.retention --days 365   # a different period for this run

Run it weekly from Cloud Scheduler / a Cloud Run job. "Last used" is `users.last_seen_at`, refreshed on a farmer's
first request each day, or the day the account was created if they never came back. Erasure is the same code as the
farmer's own "Delete my data" (services/account.py), so plots, soil samples, snapshots, jobs, consent history and the
Firebase sign-in all go. The log keeps only a hash of each id.
"""

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db import SessionLocal, init_db
from app.models import User
from app.services import account

logger = logging.getLogger(__name__)


def run(*, days: int | None = None, apply: bool = False, limit: int | None = None) -> dict:
    days = days if days is not None else get_settings().retention_days
    if days < 30:
        raise ValueError("Refusing a retention period under 30 days: it would erase farmers who simply skipped a season.")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with SessionLocal() as db:
        stmt = (select(User.uid).where(func.coalesce(User.last_seen_at, User.created_at) < cutoff)
                .order_by(User.uid))
        if limit:
            stmt = stmt.limit(limit)
        uids = list(db.scalars(stmt))
    erased = 0
    if apply:
        for uid in uids:
            with SessionLocal() as db:  # one account at a time, so one failure cannot undo the others
                user = db.get(User, uid)
                if user is None:
                    continue
                try:
                    account.erase(db, user)
                    erased += 1
                except Exception:  # noqa: BLE001
                    logger.exception("retention: could not erase an account")
                    db.rollback()
    return {"retention_days": days, "cutoff": cutoff.isoformat(), "due": len(uids), "erased": erased,
            "applied": apply}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, help="override RETENTION_DAYS")
    ap.add_argument("--apply", action="store_true", help="actually erase (without this it only reports)")
    ap.add_argument("--limit", type=int, help="stop after this many accounts")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    print(json.dumps(run(days=args.days, apply=args.apply, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
