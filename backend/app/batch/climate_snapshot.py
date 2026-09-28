"""Nightly job: compute satellite and climate conditions for every plot in bulk and store them for the API to read.

    python -m app.batch.climate_snapshot                 # every plot without a snapshot from today
    python -m app.batch.climate_snapshot --limit 2000    # a trial run
    python -m app.batch.climate_snapshot --force         # recompute everything, even today's rows

Why: Earth Engine answers one plot in 10 to 30 s, so serving it per request cannot work for a national user base.
Asked about hundreds of plots at once it takes about as long as for one, so this job walks the plots in chunks
(EE_BATCH_CHUNK, sorted by area so each chunk's satellite scenes overlap), asks Earth Engine once per chunk, and
writes one row per plot into `climate_snapshots`. The API then does a primary-key lookup (services/climate_snapshots.py).

Run it from Cloud Scheduler / a Cloud Run job after the day's CHIRPS/ERA5 data has landed (early morning is fine), with
Earth Engine credentials. It is safe to run twice, to stop half way, and to run in several copies at once on
different --shard values: every plot's row is written independently. If a chunk fails it is split in halves until the
plot that breaks it is found and skipped, so one bad polygon cannot stop the night.

At tens of millions of plots, switch the compute step to an Earth Engine batch export (`Export.table.toBigQuery`) and
load the result into this table; the table and the API lookup stay the same. BIGQUERY_TABLE additionally appends every
night's rows to a BigQuery table for analytics and for training the seasonal model.
"""

import argparse
import json
import logging
import time
import zlib
from datetime import date, datetime, timezone

from sqlalchemy import and_, delete, func, or_, select

from app.core.config import get_settings
from app.db import SessionLocal, init_db
from app.models import ClimateSnapshot, Plot
from app.services.climate_snapshots import plot_key

logger = logging.getLogger(__name__)

KEEP_DAYS = 7  # snapshots older than this belong to deleted or redrawn plots and are removed


class BigQuerySink:
    """Appends each night's rows to a BigQuery table (streaming insert). Failures are logged, never fatal."""

    def __init__(self, table: str) -> None:
        from google.cloud import bigquery

        self.table = table
        self._client = bigquery.Client()

    def write(self, rows: list[dict]) -> None:
        try:
            errors = self._client.insert_rows_json(self.table, rows)
            if errors:
                logger.warning("BigQuery rejected %d rows, e.g. %s", len(errors), errors[0])
        except Exception:  # noqa: BLE001 - analytics must never fail the night's job
            logger.warning("BigQuery insert failed", exc_info=True)


def _next_chunk(db, window_days: int, today: date, after: tuple[str, str] | None, size: int, force: bool,
                shard: tuple[int, int] | None) -> list[Plot]:
    cell = func.coalesce(Plot.cell_id, "")
    q = select(Plot).outerjoin(ClimateSnapshot, and_(ClimateSnapshot.plot_id == Plot.id,
                                                     ClimateSnapshot.window_days == window_days))
    if not force:
        q = q.where(or_(ClimateSnapshot.plot_id.is_(None), ClimateSnapshot.computed_on < today))
    if after is not None:
        q = q.where(or_(cell > after[0], and_(cell == after[0], Plot.id > after[1])))
    q = q.order_by(cell, Plot.id)
    if shard is None:
        return list(db.scalars(q.limit(size)))
    # Sharding: scan on and keep only this shard's plots, so copies never work on the same plot.
    kept: list[Plot] = []
    while len(kept) < size:
        page = list(db.scalars(q.limit(size * shard[1])))
        if not page:
            break
        kept += [p for p in page if zlib.crc32(p.id.encode()) % shard[1] == shard[0]]
        q = q.where(or_(cell > (page[-1].cell_id or ""), and_(cell == (page[-1].cell_id or ""), Plot.id > page[-1].id)))
        if len(page) < size * shard[1]:
            break
    return kept[:size]


def compute(provider, plots: list[Plot], window_days: int, failed: list[str]) -> dict:
    """{plot_key: ObservedClimate} for the plots. A failing chunk is halved until the culprit plot is isolated."""
    by_key = {plot_key(p.corner_points): p for p in plots}
    todo = [(k, p.corner_points) for k, p in by_key.items()]
    try:
        return provider.observed_many(todo, window_days)
    except Exception as exc:  # noqa: BLE001
        if len(todo) == 1:
            logger.warning("plot %s skipped: %s", by_key[todo[0][0]].id, exc)
            failed.append(by_key[todo[0][0]].id)
            return {}
        logger.warning("chunk of %d plots failed (%s); retrying in halves", len(todo), exc)
        half = len(plots) // 2
        return {**compute(provider, plots[:half], window_days, failed), **compute(provider, plots[half:], window_days, failed)}


def _row(plot: Plot, key: str, o, window_days: int, today: date) -> ClimateSnapshot:
    return ClimateSnapshot(
        plot_key=key, window_days=window_days, plot_id=plot.id, computed_on=today, as_of=o.as_of, rain_mm=o.rain_mm,
        rain_normal_mm=o.rain_normal_mm, tmean_c=o.tmean_c, ndvi=o.ndvi, ndvi_normal=o.ndvi_normal,
        soil_moisture_pct=o.soil_moisture_pct, sources=list(o.sources), computed_at=datetime.now(timezone.utc))


def _bq_row(r: ClimateSnapshot) -> dict:
    return {"plot_key": r.plot_key, "window_days": r.window_days, "computed_on": r.computed_on.isoformat(),
            "as_of": r.as_of.isoformat() if r.as_of else None, "rain_mm": r.rain_mm, "rain_normal_mm": r.rain_normal_mm,
            "tmean_c": r.tmean_c, "ndvi": r.ndvi, "ndvi_normal": r.ndvi_normal,
            "soil_moisture_pct": r.soil_moisture_pct, "sources": r.sources}


def run(*, window_days: int = 30, chunk: int | None = None, limit: int | None = None, force: bool = False,
        provider=None, sink=None, today: date | None = None, shard: tuple[int, int] | None = None) -> dict:
    """Refresh the snapshots. Returns a summary. `provider` needs `observed_many(plots, window_days)`."""
    s = get_settings()
    chunk = chunk or s.ee_batch_chunk
    today = today or date.today()
    if provider is None:
        from app.core.gee_auth import init_earth_engine, is_earth_engine_ready
        from app.providers.gee_provider import GEEProvider

        init_earth_engine()
        if not is_earth_engine_ready():
            raise RuntimeError("Earth Engine is not available: set GEE_CLOUD_PROJECT and credentials for this job.")
        provider = GEEProvider()
    if sink is None and s.bigquery_table:
        sink = BigQuerySink(s.bigquery_table)

    started = time.monotonic()
    seen = written = chunks = 0
    failed: list[str] = []
    after: tuple[str, str] | None = None
    while limit is None or seen < limit:
        size = chunk if limit is None else min(chunk, limit - seen)
        with SessionLocal() as db:
            plots = _next_chunk(db, window_days, today, after, size, force, shard)
            if not plots:
                break
            after = (plots[-1].cell_id or "", plots[-1].id)
            seen += len(plots)
            chunks += 1
            results = compute(provider, plots, window_days, failed)
            rows = []
            for p in plots:
                key = plot_key(p.corner_points)
                if key in results:
                    rows.append(db.merge(_row(p, key, results[key], window_days, today)))
            db.commit()
            written += len(rows)
            if sink is not None and rows:
                sink.write([_bq_row(r) for r in rows])
        logger.info("chunk %d: %d plots, %d written so far", chunks, len(plots), written)
    with SessionLocal() as db:
        purged = db.execute(delete(ClimateSnapshot).where(ClimateSnapshot.computed_on < date.fromordinal(
            today.toordinal() - KEEP_DAYS)).execution_options(synchronize_session=False)).rowcount
        db.commit()
    return {"plots_seen": seen, "snapshots_written": written, "plots_failed": len(failed), "failed_plot_ids": failed[:20],
            "chunks": chunks, "purged_old_snapshots": purged, "seconds": round(time.monotonic() - started, 1)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--chunk", type=int, help="plots per Earth Engine request (default EE_BATCH_CHUNK)")
    ap.add_argument("--limit", type=int, help="stop after this many plots")
    ap.add_argument("--force", action="store_true", help="recompute plots that already have today's snapshot")
    ap.add_argument("--shard", help="I/N: only plots whose id hashes to I of N, to run N copies side by side")
    args = ap.parse_args()
    shard = tuple(int(x) for x in args.shard.split("/")) if args.shard else None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()  # migrates a SQLite dev database; Postgres is migrated by `python -m app.migrate`
    summary = run(window_days=args.window_days, chunk=args.chunk, limit=args.limit, force=args.force, shard=shard)
    print(json.dumps(summary, indent=2))
    raise SystemExit(1 if summary["plots_failed"] and not summary["snapshots_written"] else 0)


if __name__ == "__main__":
    main()
