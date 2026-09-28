"""Read side of the nightly satellite snapshots (see models.ClimateSnapshot and app/batch/climate_snapshot.py)."""

import hashlib
import logging
from datetime import date, timedelta

from sqlalchemy import delete

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import ClimateSnapshot
from app.providers.base import ObservedClimate

logger = logging.getLogger(__name__)


def plot_key(corners) -> str:
    """Stable id of a plot's shape: its corners rounded to ~1 m. The same field always maps to the same row, and a
    re-drawn field maps to a different one."""
    rounded = tuple((round(lat, 5), round(lon, 5)) for lat, lon in corners)
    return hashlib.sha1(repr(rounded).encode()).hexdigest()


def to_observed(row: ClimateSnapshot) -> ObservedClimate:
    return ObservedClimate(
        window_days=row.window_days, rain_mm=row.rain_mm, rain_normal_mm=row.rain_normal_mm, tmean_c=row.tmean_c,
        ndvi=row.ndvi, ndvi_normal=row.ndvi_normal, soil_moisture_pct=row.soil_moisture_pct, as_of=row.as_of,
        sources=list(row.sources or []))


def lookup(corners, window_days: int) -> ObservedClimate | None:
    """Last night's numbers for this plot, or None if there are none or they are older than
    EE_SNAPSHOT_MAX_AGE_DAYS (so a batch that stopped running is noticed, not silently served for weeks)."""
    oldest = date.today() - timedelta(days=get_settings().ee_snapshot_max_age_days)
    try:
        with SessionLocal() as db:
            row = db.get(ClimateSnapshot, (plot_key(corners), window_days))
            if row is None or row.computed_on < oldest:
                return None
            return to_observed(row)
    except Exception:  # noqa: BLE001 - a snapshot problem must never break a request: fall through to live data
        logger.warning("snapshot lookup failed", exc_info=True)
        return None


def erase_for_plots(db, plot_ids: list[str]) -> None:
    """Remove the snapshots of deleted plots (called by plot deletion and account erasure)."""
    if plot_ids:
        db.execute(delete(ClimateSnapshot).where(ClimateSnapshot.plot_id.in_(plot_ids))
                   .execution_options(synchronize_session=False))
