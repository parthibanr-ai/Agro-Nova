"""Nightly satellite snapshots: the batch job, the API's lookup, freshness, failure handling, erasure.

The batch is exercised with a fake Earth Engine (`observed_many`) that records how it was called. One real-Earth-Engine
test runs only when TEST_EARTH_ENGINE=1 (it needs credentials and takes about a minute).
"""

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.batch import climate_snapshot
from app.core.config import get_settings
from app.db import SessionLocal
from app.models import ClimateSnapshot, Plot, User
from app.providers import registry
from app.providers.base import ObservedClimate
from app.services import climate_snapshots
from tests.conftest import NASHIK

TODAY = date.today()


def _corners(i: int) -> list[dict]:
    lat, lon = 30.0 + (i // 40) * 0.05, 74.5 + (i % 40) * 0.05  # one plot per ~5 km cell
    d = 0.0006
    return [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + d}, {"lat": lat + d, "lon": lon + d},
            {"lat": lat + d, "lon": lon}]


@pytest.fixture()
def plots(client):
    """`make(n)` registers n plots for n farmers and returns their ids."""

    def make(n, start=0):
        ids = []
        for i in range(start, start + n):
            h = {"X-Dev-User": f"snap-farmer-{i}"}
            ids.append(client.post("/api/v1/plots", json={"name": "P", "crop": "wheat", "country": "IN",
                                                          "state": "Punjab", "corners": _corners(i)},
                                   headers=h).json()["id"])
        return ids

    return make


class FakeEarthEngine:
    """Answers observed_many() like the real provider would, and remembers each call."""

    def __init__(self, fail_for: set[str] | None = None, rain=42.5):
        self.calls: list[list[str]] = []
        self.fail_for = fail_for or set()  # plot keys that make any chunk containing them fail
        self.rain = rain

    def observed_many(self, plots, window_days):
        self.calls.append([k for k, _ in plots])
        if self.fail_for & {k for k, _ in plots}:
            raise RuntimeError("Earth Engine: geometry too complex")
        return {k: ObservedClimate(window_days, rain_mm=self.rain, rain_normal_mm=100.0, tmean_c=29.5, ndvi=0.41,
                                   ndvi_normal=0.5, soil_moisture_pct=12.5, as_of=TODAY - timedelta(days=1),
                                   sources=["CHIRPS", "Sentinel-2"])
                for k, _ in plots}


def _snapshots():
    with SessionLocal() as db:
        return list(db.scalars(select(ClimateSnapshot)))


def _wipe():
    with SessionLocal() as db:
        db.query(ClimateSnapshot).delete()
        db.query(Plot).delete()
        db.query(User).delete()
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


# ------------------------------------------------------------------ the batch
def test_one_request_to_earth_engine_covers_a_whole_chunk(plots):
    plots(12)
    ee = FakeEarthEngine()
    summary = climate_snapshot.run(provider=ee, chunk=5)
    assert summary["plots_seen"] == 12 and summary["snapshots_written"] == 12 and summary["chunks"] == 3
    assert [len(c) for c in ee.calls] == [5, 5, 2]  # 3 Earth Engine requests for 12 plots, not 12 x 7
    assert len(_snapshots()) == 12


def test_stored_values_round_trip_to_what_the_api_needs(plots):
    (pid,) = plots(1)
    climate_snapshot.run(provider=FakeEarthEngine())
    with SessionLocal() as db:
        plot = db.get(Plot, pid)
        corners = plot.corner_points
    got = climate_snapshots.lookup(corners, 30)
    assert got == ObservedClimate(30, rain_mm=42.5, rain_normal_mm=100.0, tmean_c=29.5, ndvi=0.41, ndvi_normal=0.5,
                                  soil_moisture_pct=12.5, as_of=TODAY - timedelta(days=1),
                                  sources=["CHIRPS", "Sentinel-2"])
    assert climate_snapshots.lookup(corners, 14) is None  # a different window was never computed


def test_plots_are_sent_in_area_order_so_a_chunks_scenes_overlap(plots):
    ids = plots(6)
    ee = FakeEarthEngine()
    climate_snapshot.run(provider=ee, chunk=100)
    with SessionLocal() as db:
        cells = [db.get(Plot, i).cell_id for i in ids]
    sent = [k for k in ee.calls[0]]
    with SessionLocal() as db:
        by_key = {climate_snapshots.plot_key(p.corner_points): p.cell_id or "" for p in db.scalars(select(Plot))}
    assert [by_key[k] for k in sent] == sorted(by_key[k] for k in sent)
    assert len(set(cells)) == 6


def test_running_again_only_does_the_plots_that_are_missing(plots):
    plots(5)
    ee = FakeEarthEngine()
    climate_snapshot.run(provider=ee)
    more = plots(3, start=5)
    again = FakeEarthEngine()
    summary = climate_snapshot.run(provider=again)
    assert summary["plots_seen"] == 3 and sum(len(c) for c in again.calls) == 3
    assert len(more) == 3 and len(_snapshots()) == 8
    assert climate_snapshot.run(provider=FakeEarthEngine())["plots_seen"] == 0  # nothing left for today


def test_tomorrow_refreshes_everything_and_force_redoes_today(plots):
    plots(4)
    climate_snapshot.run(provider=FakeEarthEngine(rain=10.0))
    assert climate_snapshot.run(provider=FakeEarthEngine(), force=True)["plots_seen"] == 4
    tomorrow = FakeEarthEngine(rain=77.0)
    assert climate_snapshot.run(provider=tomorrow, today=TODAY + timedelta(days=1))["plots_seen"] == 4
    assert {s.rain_mm for s in _snapshots()} == {77.0}
    assert len(_snapshots()) == 4  # updated in place, not appended


def test_one_bad_polygon_cannot_stop_the_night(plots):
    ids = plots(9)
    with SessionLocal() as db:
        bad = climate_snapshots.plot_key(db.get(Plot, ids[4]).corner_points)
    ee = FakeEarthEngine(fail_for={bad})
    summary = climate_snapshot.run(provider=ee, chunk=9)
    assert summary["snapshots_written"] == 8 and summary["plots_failed"] == 1
    assert summary["failed_plot_ids"] == [ids[4]]
    assert bad not in {s.plot_key for s in _snapshots()}
    assert len(ee.calls) < 9 + 8  # halved to find it, not one call per plot


def test_a_limit_makes_a_trial_run(plots):
    plots(10)
    summary = climate_snapshot.run(provider=FakeEarthEngine(), limit=4, chunk=3)
    assert summary["plots_seen"] == 4 and len(_snapshots()) == 4


def test_copies_running_side_by_side_share_the_work_without_overlap(plots):
    plots(20)
    a, b = FakeEarthEngine(), FakeEarthEngine()
    climate_snapshot.run(provider=a, chunk=4, shard=(0, 2))
    climate_snapshot.run(provider=b, chunk=4, shard=(1, 2))
    keys_a, keys_b = {k for c in a.calls for k in c}, {k for c in b.calls for k in c}
    assert keys_a and keys_b and not (keys_a & keys_b) and len(keys_a | keys_b) == 20
    assert len(_snapshots()) == 20


def test_old_snapshots_of_deleted_or_redrawn_plots_are_purged(plots):
    plots(2)
    climate_snapshot.run(provider=FakeEarthEngine())
    with SessionLocal() as db:
        db.add(ClimateSnapshot(plot_key="orphan", window_days=30, plot_id="gone", sources=[],
                               computed_on=TODAY - timedelta(days=climate_snapshot.KEEP_DAYS + 1)))
        db.commit()
    summary = climate_snapshot.run(provider=FakeEarthEngine())
    assert summary["purged_old_snapshots"] == 1 and "orphan" not in {s.plot_key for s in _snapshots()}


def test_the_bigquery_copy_gets_each_nights_rows_and_its_failure_is_harmless(plots):
    plots(3)
    written = []

    class Sink:
        def write(self, rows):
            written.extend(rows)

    climate_snapshot.run(provider=FakeEarthEngine(), sink=Sink())
    assert len(written) == 3 and {"plot_key", "rain_mm", "ndvi", "computed_on", "sources"} <= set(written[0])
    assert written[0]["computed_on"] == TODAY.isoformat()

    class BrokenClient:
        def insert_rows_json(self, table, rows):
            raise RuntimeError("quota exceeded")

    sink = climate_snapshot.BigQuerySink.__new__(climate_snapshot.BigQuerySink)
    sink.table, sink._client = "p.d.t", BrokenClient()
    sink.write([{"a": 1}])  # logged, not raised


def test_the_batch_refuses_to_run_without_earth_engine(plots, monkeypatch):
    plots(1)
    monkeypatch.setattr(get_settings(), "gee_cloud_project", None)
    from app.core import gee_auth

    monkeypatch.setattr(gee_auth, "_initialized", False)
    with pytest.raises(RuntimeError, match="Earth Engine is not available"):
        climate_snapshot.run()


# ------------------------------------------------------------------ the API side
def test_the_api_reads_the_snapshot_and_never_calls_earth_engine(plots, monkeypatch):
    (pid,) = plots(1)
    climate_snapshot.run(provider=FakeEarthEngine(rain=55.5))
    with SessionLocal() as db:
        corners = db.get(Plot, pid).corner_points

    class Explode:
        def observed(self, *a):
            raise AssertionError("live Earth Engine must not be called when a snapshot exists")

    provider = registry.ResilientProvider()
    provider._gee = Explode()
    monkeypatch.setattr(registry, "is_earth_engine_ready", lambda: True)
    got = provider.observed(corners, 30)
    assert got.rain_mm == 55.5 and got.ndvi == 0.41 and "Sentinel-2" in got.sources


def test_snapshots_work_on_instances_without_earth_engine_credentials(plots, monkeypatch):
    (pid,) = plots(1)
    climate_snapshot.run(provider=FakeEarthEngine(rain=55.5))
    with SessionLocal() as db:
        corners = db.get(Plot, pid).corner_points
    provider = registry.ResilientProvider()
    monkeypatch.setattr(registry, "is_earth_engine_ready", lambda: False)
    assert provider.observed(corners, 30).ndvi == 0.41


class _CountingLive:
    def __init__(self):
        self.calls = 0

    def observed(self, corners, window_days):
        self.calls += 1
        return ObservedClimate(window_days, rain_mm=1.0, rain_normal_mm=2.0, ndvi=0.3, as_of=TODAY, sources=["live"])


class _WeatherOnly:
    def observed(self, corners, window_days):
        return ObservedClimate(window_days, rain_mm=9.0, rain_normal_mm=90.0, tmean_c=31, sources=["Open-Meteo"])


def _provider(monkeypatch, live):
    p = registry.ResilientProvider()
    p._gee, p._fallback = live, _WeatherOnly()
    monkeypatch.setattr(registry, "is_earth_engine_ready", lambda: True)
    return p


def test_a_plot_without_a_snapshot_is_fetched_live_once_by_default(monkeypatch):
    live = _CountingLive()
    provider = _provider(monkeypatch, live)
    corners = [(30.1, 74.6), (30.1, 74.601), (30.101, 74.601)]
    provider.observed(corners, 30)
    provider.observed(corners, 30)
    assert live.calls == 1


def test_with_live_fallback_off_a_new_plot_gets_shared_weather_until_the_batch_runs(monkeypatch):
    monkeypatch.setattr(get_settings(), "ee_live_fallback", False)
    live = _CountingLive()
    provider = _provider(monkeypatch, live)
    got = provider.observed([(30.1, 74.6), (30.1, 74.601), (30.101, 74.601)], 30)
    assert live.calls == 0 and got.rain_mm == 9.0 and got.sources == ["Open-Meteo"]


def test_a_stale_snapshot_is_not_served(plots, monkeypatch):
    (pid,) = plots(1)
    climate_snapshot.run(provider=FakeEarthEngine(), today=TODAY - timedelta(days=5))
    with SessionLocal() as db:
        corners = db.get(Plot, pid).corner_points
    assert climate_snapshots.lookup(corners, 30) is None  # the batch stopped running: do not serve week-old numbers
    monkeypatch.setattr(get_settings(), "ee_snapshot_max_age_days", 7)
    assert climate_snapshots.lookup(corners, 30) is not None


def test_a_redrawn_plot_does_not_get_the_old_shapes_numbers(plots):
    (pid,) = plots(1)
    climate_snapshot.run(provider=FakeEarthEngine())
    with SessionLocal() as db:
        corners = db.get(Plot, pid).corner_points
    moved = [(lat + 0.001, lon) for lat, lon in corners]
    assert climate_snapshots.lookup(corners, 30) is not None and climate_snapshots.lookup(moved, 30) is None


def test_a_database_error_in_the_lookup_falls_through_instead_of_failing_the_request(monkeypatch):
    def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr(climate_snapshots, "SessionLocal", boom)
    assert climate_snapshots.lookup([(30.1, 74.6)], 30) is None


# ------------------------------------------------------------------ erasure
def test_deleting_a_plot_or_an_account_removes_its_snapshots(client, plots):
    a, b = plots(2)
    climate_snapshot.run(provider=FakeEarthEngine())
    assert len(_snapshots()) == 2
    assert client.delete(f"/api/v1/plots/{a}", headers={"X-Dev-User": "snap-farmer-0"}).status_code == 204
    assert {s.plot_id for s in _snapshots()} == {b}
    assert client.delete("/api/v1/me?confirm=true", headers={"X-Dev-User": "snap-farmer-1"}).status_code == 204
    assert _snapshots() == []


# ------------------------------------------------------------------ the real thing (needs credentials)
@pytest.mark.skipif(os.environ.get("TEST_EARTH_ENGINE") != "1", reason="set TEST_EARTH_ENGINE=1 to call real Earth Engine")
def test_the_batch_query_agrees_with_the_one_plot_query_on_real_earth_engine():
    from app.core.gee_auth import init_earth_engine
    from app.providers.gee_provider import GEEProvider

    init_earth_engine()
    ee = GEEProvider()
    plots = [(f"k{i}", [(lat, lon), (lat, lon + 0.0009), (lat + 0.0009, lon + 0.0009), (lat + 0.0009, lon)])
             for i, (lat, lon) in enumerate([(30.9010, 75.8570), (30.9500, 75.9000), (31.0500, 76.1000)])]
    many = ee.observed_many(plots, 30)
    for key, corners in plots:
        one = ee.observed(corners, 30)
        got = many[key]
        assert got.as_of == one.as_of
        for field in ("rain_mm", "rain_normal_mm", "tmean_c", "soil_moisture_pct"):
            a, b = getattr(got, field), getattr(one, field)
            assert (a is None) == (b is None), field
            if a is not None:
                assert a == pytest.approx(b, rel=0.02, abs=0.2), (field, a, b)
        if one.ndvi is not None and got.ndvi is not None:
            assert got.ndvi == pytest.approx(one.ndvi, abs=0.03)
