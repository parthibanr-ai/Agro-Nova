import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTH_MODE"] = "dev"
os.environ["TRANSLATE_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""  # tests must never reach a real model, whatever is in .env
os.environ["ANTHROPIC_API_KEY"] = ""

from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import ClimateProvider, DailyForecast, ObservedClimate  # noqa: E402
from app.services.enso import EnsoState  # noqa: E402

# A ~1.1 ha square plot near Nashik, India (walking order).
NASHIK = [
    {"lat": 19.9975, "lon": 73.7898},
    {"lat": 19.9975, "lon": 73.7908},
    {"lat": 19.9985, "lon": 73.7908},
    {"lat": 19.9985, "lon": 73.7898},
]


class FakeProvider(ClimateProvider):
    name = "fake"

    def observed(self, corners, window_days):
        return ObservedClimate(window_days, rain_mm=20, rain_normal_mm=100, tmean_c=30, sources=["fake"])

    def forecast(self, lat, lon, days):
        return [DailyForecast(date.today() + timedelta(days=i), 37, 24, 0.0, 6.0) for i in range(days)]


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()


@pytest.fixture(autouse=True)
def _fresh_caches_and_limits():
    """Every test starts with empty caches and rate-limit counters, so tests cannot leak results into each other."""
    from app.core import cache, ratelimit

    cache.clear_all_caches()
    ratelimit.reset()


@pytest.fixture()
def client(monkeypatch):
    from app.api import routes

    monkeypatch.setattr(routes, "get_climate_provider", lambda: FakeProvider())
    monkeypatch.setattr(routes, "get_enso_state", lambda: EnsoState(True, "el_nino", "moderate", 1.2, "JJA 2026", "strengthening"))
    monkeypatch.setattr(routes.soil, "fetch_soilgrids", lambda lat, lon: {"phh2o": {"label": "pH (water)", "value": 8.6, "unit": "", "depth": "0-15 cm"}})
    return TestClient(app, headers={"X-Dev-User": "farmer-1"})
