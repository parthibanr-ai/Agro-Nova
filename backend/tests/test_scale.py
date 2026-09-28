"""Scalability guards: nothing slow or flaky upstream may stall other farmers' requests."""

import asyncio
import io
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.main import app
from tests.conftest import NASHIK, FakeProvider

HEADERS = {"X-Dev-User": "farmer-scale"}
CORNERS = [(30.9010, 75.8570), (30.9010, 75.8590), (30.9030, 75.8590), (30.9030, 75.8570)]


def _run_concurrently(slow_request, fast_request, head_start: float = 0.2):
    """Fire a slow request, then a fast one while it is still running. Returns (fast_status, fast_seconds)."""

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=HEADERS) as c:
            slow = asyncio.create_task(slow_request(c))
            # Measure from when the fast request is *due*, not from when the loop next gets to run it:
            # a blocked event loop delays the wake-up itself, which is exactly what we want to catch.
            due = time.perf_counter() + head_start
            await asyncio.sleep(head_start)
            resp = await fast_request(c)
            took = time.perf_counter() - due
            slow_resp = await slow
            assert slow_resp.status_code == 200, slow_resp.text
            return resp.status_code, took

    return asyncio.run(go())


# ------------------------------------------------------------------ event loop
def test_slow_diagnosis_does_not_freeze_other_requests(monkeypatch):
    from app.services import diagnosis

    def slow_diagnose(*a, **k):
        time.sleep(1.5)  # stands in for Gemini retries
        return diagnosis.assemble({"status": "healthy", "confidence": 0.9})

    monkeypatch.setattr(diagnosis, "diagnose", slow_diagnose)
    files = {"image": ("leaf.jpg", io.BytesIO(b"\xff\xd8fake"), "image/jpeg")}

    status, took = _run_concurrently(
        lambda c: c.post("/api/v1/diagnosis", files=files),
        lambda c: c.get("/api/v1/meta/countries"),
    )
    assert status == 200
    assert took < 0.5, f"a fast request waited {took:.2f}s behind a slow diagnosis"


def test_slow_translation_does_not_freeze_other_requests(monkeypatch):
    import app.main as main

    def slow_localize(payload, lang):
        time.sleep(1.5)  # stands in for the Cloud Translation round trip
        return payload

    monkeypatch.setattr(main, "localize_payload", slow_localize)

    status, took = _run_concurrently(
        lambda c: c.get("/api/v1/meta/countries?lang=hi"),
        lambda c: c.get("/api/v1/health"),
    )
    assert status == 200
    assert took < 0.5, f"a fast request waited {took:.2f}s behind a translation"


# ------------------------------------------------------------------ first login
def test_concurrent_first_requests_for_a_new_user_all_succeed(tmp_path):
    """The app fires several requests at start-up; a brand-new user must not get a 500 from the create race.

    Uses a file database: the shared in-memory test engine has one connection, so threads would trample it.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models  # noqa: F401  (register tables)
    from app.db import Base, get_db

    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def db_dep():
        db = make_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_dep
    try:
        client = TestClient(app, headers={"X-Dev-User": f"new-user-{time.time_ns()}"})
        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(lambda _: client.get("/api/v1/me").status_code, range(24)))
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
    assert codes == [200] * 24


# ------------------------------------------------------------------ database connections
def test_db_connection_is_released_before_slow_external_calls(client, monkeypatch):
    """A request must not hold a pooled DB connection while it waits on Earth Engine, Gemini or SoilGrids."""
    from app.api import routes
    from app.db import get_db
    from app.services import crop_recommendation

    sessions = []

    def spy_db():
        for db in get_db():
            sessions.append(db)
            yield db

    in_txn: list[tuple[str, bool]] = []

    class SpyProvider(FakeProvider):
        def observed(self, corners, window_days):
            in_txn.append(("observed", sessions[-1].in_transaction()))
            return super().observed(corners, window_days)

        def forecast(self, lat, lon, days):
            in_txn.append(("forecast", sessions[-1].in_transaction()))
            return super().forecast(lat, lon, days)

    def spy_soilgrids(lat, lon):
        in_txn.append(("soilgrids", sessions[-1].in_transaction()))
        return None

    def spy_gemini(prompt):
        in_txn.append(("gemini", sessions[-1].in_transaction()))
        return {"season": "Rabi", "recommendations": []}

    body = {"name": "P", "crop": "wheat", "country": "IN", "corners": NASHIK}
    plot_id = client.post("/api/v1/plots", json=body).json()["id"]

    app.dependency_overrides[get_db] = spy_db
    try:
        monkeypatch.setattr(routes, "get_climate_provider", lambda: SpyProvider())
        monkeypatch.setattr(routes.soil, "fetch_soilgrids", spy_soilgrids)
        monkeypatch.setattr(crop_recommendation, "call_gemini", spy_gemini)
        client.get(f"/api/v1/plots/{plot_id}/forecast")
        client.get(f"/api/v1/plots/{plot_id}/soil")
        client.get(f"/api/v1/plots/{plot_id}/crop-recommendation")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert {"observed", "forecast", "soilgrids", "gemini"} <= {name for name, _ in in_txn}, in_txn
    still_open = [name for name, held in in_txn if held]
    assert not still_open, f"DB transaction still open during: {still_open}"


# ------------------------------------------------------------------ Gemini client
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def fake_gemini(monkeypatch):
    """Replace the google-genai client with one whose behaviour and latency the test controls."""
    from google import genai
    from google.genai import errors

    from app.services import gemini_client

    gemini_client.reset_state()
    clock = _Clock()
    monkeypatch.setattr(gemini_client.time, "monotonic", clock.now)
    monkeypatch.setattr(gemini_client.time, "sleep", clock.sleep)
    monkeypatch.setattr(gemini_client.random, "uniform", lambda a, b: 1.0)

    class State:
        calls: list[str] = []
        created = 0
        fail_with: int | None = 503
        call_seconds = 3.0

    class Models:
        def generate_content(self, model, contents, config):
            State.calls.append(model)
            clock.t += State.call_seconds
            if State.fail_with:
                raise errors.APIError(State.fail_with, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
            return type("R", (), {"text": '{"ok": true}'})()

    class Client:
        def __init__(self, **kw):
            State.created += 1
            self.models = Models()

    State.calls = []
    State.created = 0
    monkeypatch.setattr(genai, "Client", Client)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    State.clock = clock
    yield State
    gemini_client.reset_state()
    get_settings.cache_clear()


def test_gemini_outage_is_bounded_in_time_and_calls(fake_gemini):
    from app.services import gemini_client

    start = fake_gemini.clock.now()
    with pytest.raises(Exception):
        gemini_client.generate_json(["hi"], 0.2)
    elapsed = fake_gemini.clock.now() - start
    # Before: 3 models x 3 attempts = 9 calls and ~30 s. Now bounded by the request budget.
    assert elapsed <= gemini_client.TOTAL_BUDGET_S + fake_gemini.call_seconds
    assert len(fake_gemini.calls) < 9


def test_gemini_circuit_opens_and_later_requests_fail_fast(fake_gemini):
    from app.services import gemini_client

    for _ in range(4):
        with pytest.raises(Exception):
            gemini_client.generate_json(["hi"], 0.2)
    calls_before = len(fake_gemini.calls)
    t0 = fake_gemini.clock.now()
    with pytest.raises(gemini_client.GeminiUnavailable):
        gemini_client.generate_json(["hi"], 0.2)
    assert len(fake_gemini.calls) == calls_before, "an open circuit must not call Gemini"
    assert fake_gemini.clock.now() - t0 < 0.1


def test_gemini_circuit_recovers_after_cooldown(fake_gemini):
    from app.services import gemini_client

    for _ in range(4):
        with pytest.raises(Exception):
            gemini_client.generate_json(["hi"], 0.2)
    fake_gemini.clock.t += gemini_client.BREAKER_COOLDOWN_S + 1
    fake_gemini.fail_with = None
    assert gemini_client.generate_json(["hi"], 0.2) == {"ok": True}


def test_gemini_client_is_created_once(fake_gemini):
    from app.services import gemini_client

    fake_gemini.fail_with = None
    for _ in range(5):
        gemini_client.generate_json(["hi"], 0.2)
    assert fake_gemini.created == 1


def test_gemini_non_retryable_errors_are_raised_immediately(fake_gemini):
    from google.genai import errors

    from app.services import gemini_client

    fake_gemini.fail_with = 400
    with pytest.raises(errors.APIError):
        gemini_client.generate_json(["hi"], 0.2)
    assert len(fake_gemini.calls) == 1


# ------------------------------------------------------------------ ENSO
def test_enso_failure_is_remembered_instead_of_retried_on_every_request(monkeypatch):
    from app.services import enso

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("NOAA down")

    monkeypatch.setattr(enso.httpx, "get", boom)
    monkeypatch.setattr(enso, "_cache", None)
    monkeypatch.setattr(enso, "_failed_at", None)
    for _ in range(5):
        assert enso.get_enso_state().available is False
    assert len(calls) == 1


# ------------------------------------------------------------------ climate provider selection
def _resilient(monkeypatch, gee, fallback, ready=True):
    from app.providers import registry

    monkeypatch.setattr(registry, "is_earth_engine_ready", lambda: ready)
    p = registry.ResilientProvider()
    p._gee, p._fallback = gee, fallback
    return p


class _Fixed:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def observed(self, corners, window_days):
        if self.error:
            raise self.error
        return self.result


def _obs(**kw):
    from app.providers.base import ObservedClimate

    return ObservedClimate(30, **kw)


def test_fresh_earth_engine_result_is_used_as_is(monkeypatch):
    from datetime import date, timedelta

    gee = _obs(rain_mm=50, rain_normal_mm=80, tmean_c=30, ndvi=0.4, as_of=date.today() - timedelta(days=2))
    p = _resilient(monkeypatch, _Fixed(gee), _Fixed(_obs(rain_mm=1, rain_normal_mm=1)))
    assert p.observed(CORNERS, 30) == gee


def test_stale_satellite_rain_uses_fresh_rain_but_keeps_ndvi_and_soil_moisture(monkeypatch):
    from datetime import date, timedelta

    stale = _obs(rain_mm=249, rain_normal_mm=237, tmean_c=29, ndvi=0.31, ndvi_normal=0.4, soil_moisture_pct=11.6,
                 as_of=date.today() - timedelta(days=28), sources=["CHIRPS"])
    fresh = _obs(rain_mm=38, rain_normal_mm=139, tmean_c=31, sources=["Open-Meteo archive (ERA5)"])
    out = _resilient(monkeypatch, _Fixed(stale), _Fixed(fresh)).observed(CORNERS, 30)
    assert (out.rain_mm, out.rain_normal_mm, out.tmean_c) == (38, 139, 31)  # this month, not last month
    assert (out.ndvi, out.ndvi_normal, out.soil_moisture_pct) == (0.31, 0.4, 11.6)
    assert "Sentinel-2" in out.sources and "NASA SMAP" in out.sources and "CHIRPS" not in out.sources


def test_earth_engine_failure_or_absence_falls_back(monkeypatch):
    fresh = _obs(rain_mm=38, rain_normal_mm=139)
    assert _resilient(monkeypatch, _Fixed(error=RuntimeError("EE down")), _Fixed(fresh)).observed(CORNERS, 30) == fresh
    assert _resilient(monkeypatch, _Fixed(_obs()), _Fixed(fresh), ready=False).observed(CORNERS, 30) == fresh
