"""Count the upstream calls a crowd of farmers causes, with caching off (how it used to work) and on.

Runs the real FastAPI app in-process with fake upstreams that only count calls (no network, no cost):

    cd backend
    .venv\\Scripts\\python scripts\\simulate_sessions.py [farmers]      # default 1000

Each farmer has one plot somewhere in Punjab and opens every screen twice (a first visit and a return visit),
in Hindi for 70% of them. The interesting numbers are Gemini, Earth Engine, Open-Meteo, SoilGrids and Translation
calls per 1,000 sessions: those are what cost money, hit quotas and cause the slow screens.
"""

import os
import random
import sys
import time
from collections import Counter
from datetime import date, timedelta

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTH_MODE"] = "dev"
os.environ["TRANSLATE_API_KEY"] = "sim"
os.environ["GEMINI_API_KEY"] = "sim"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import routes  # noqa: E402
from app.core import cache, ratelimit  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.base import DailyForecast, ObservedClimate  # noqa: E402
from app.services import crop_recommendation, personalized_advice, soil, translation  # noqa: E402
from app.services.enso import EnsoState  # noqa: E402

calls: Counter = Counter()


class FakeEE:
    def observed(self, corners, window_days):
        calls["Earth Engine observed()"] += 1
        # Satellite rain is stale (as CHIRPS is in reality), so the fresh Open-Meteo rain is used.
        return ObservedClimate(window_days, rain_mm=249, rain_normal_mm=237, tmean_c=29, ndvi=0.2, ndvi_normal=0.3,
                               soil_moisture_pct=11, as_of=date.today() - timedelta(days=28), sources=["CHIRPS"])


class FakeOpenMeteo:
    def observed(self, corners, window_days):
        calls["Open-Meteo history (6 HTTP calls each)"] += 1
        return ObservedClimate(window_days, rain_mm=38, rain_normal_mm=135, tmean_c=30, sources=["Open-Meteo"])

    def forecast(self, lat, lon, days):
        calls["Open-Meteo forecast"] += 1
        return [DailyForecast(date.today() + timedelta(days=i), 36, 23, 0.0, 6.0) for i in range(days)]


def _unique_text(n: int) -> str:
    """Real Gemini text differs on every call (temperature > 0) and is about 2,000 characters long."""
    return f"Advice #{n}: " + ("Mulch the soil, irrigate by soil moisture and watch for heat stress. " * 26)


def fake_gemini_recommendation(prompt):
    calls["Gemini"] += 1
    return {"target_season": {"local_name": "Rabi"}, "recommendations": [], "basis_summary": _unique_text(calls["Gemini"])}


def fake_gemini_advice(prompt):
    calls["Gemini"] += 1
    return {"summary": _unique_text(calls["Gemini"]), "items": [{"title": "t", "detail": "d", "why": "w"}]}


def fake_soilgrids(lat, lon):
    calls["SoilGrids"] += 1
    return {"phh2o": {"label": "pH (water)", "value": 7.0 + round(random.random(), 1), "unit": "", "depth": "0-15 cm"}}


class FakeTranslateClient:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def post(self, url, params, json):
        calls["Translation API requests"] += 1
        calls["Translation API characters"] += sum(len(t) for t in json["q"])
        body = {"data": {"translations": [{"translatedText": f"<{t}>"} for t in json["q"]]}}
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()


def run(farmers: int, cached: bool, area: tuple[float, float], seed: int = 7) -> tuple[Counter, float, int]:
    calls.clear()
    cache.clear_all_caches()
    ratelimit.reset()
    translation._CACHE.clear()
    get_settings().cache_enabled = cached
    get_settings().rate_limit_enabled = False

    p = registry.ResilientProvider()
    p._gee, p._fallback = FakeEE(), FakeOpenMeteo()
    registry.is_earth_engine_ready = lambda: True
    routes.get_climate_provider = lambda: p
    routes.get_enso_state = lambda: EnsoState(True, "el_nino", "strong", 1.8, "JJA 2026", "steady")
    soil.fetch_soilgrids = soil.fetch_soilgrids.__wrapped__ if hasattr(soil.fetch_soilgrids, "__wrapped__") else soil.fetch_soilgrids
    soil._fetch_soilgrids = fake_soilgrids
    routes.soil.fetch_soilgrids = lambda lat, lon: soil._cache.get_or_compute(
        ("sg", round(lat / 0.0025), round(lon / 0.0025)), lambda: fake_soilgrids(lat, lon), negative_ttl=300)
    crop_recommendation.call_gemini = fake_gemini_recommendation
    personalized_advice.call_gemini = fake_gemini_advice
    translation.httpx.Client = FakeTranslateClient

    random.seed(seed)  # fake_soilgrids uses the global generator: seed it so the run is repeatable
    rng = random.Random(seed)
    client = TestClient(app)
    t0 = time.perf_counter()
    requests = 0
    for i in range(farmers):
        uid = f"farmer-{i}"
        h = {"X-Dev-User": uid}
        lat, lon = 30.0 + rng.uniform(0, area[0]), 74.5 + rng.uniform(0, area[1])
        d = 0.0006
        corners = [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + d}, {"lat": lat + d, "lon": lon + d},
                   {"lat": lat + d, "lon": lon}]
        crop = rng.choice(["wheat", "wheat", "rice", "cotton", "maize"])
        lang = "hi" if rng.random() < 0.7 else "en"
        if rng.random() < 0.4:
            client.put("/api/v1/me", json={"livestock": {"cows": rng.choice([1, 2, 3])}}, headers=h)
        pid = client.post("/api/v1/plots", json={"name": "P", "crop": crop, "country": "IN", "state": "Punjab",
                                                  "corners": corners}, headers=h).json()["id"]
        for _visit in range(2):  # first visit, then a return visit
            for path in (f"/api/v1/plots/{pid}/forecast", f"/api/v1/plots/{pid}/soil",
                         f"/api/v1/plots/{pid}/crop-recommendation", f"/api/v1/resilience?plot_id={pid}",
                         f"/api/v1/water-tips?plot_id={pid}", f"/api/v1/plots/{pid}/market",
                         f"/api/v1/schemes?plot_id={pid}"):
                sep = "&" if "?" in path else "?"
                r = client.get(f"{path}{sep}lang={lang}", headers=h)
                requests += 1
                assert r.status_code == 200, (path, r.status_code, r.text[:200])
    return Counter(calls), time.perf_counter() - t0, requests


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    farmers = int(args[0]) if args else 1000
    side = float(args[1]) if len(args) > 1 else None
    area = (side, side) if side else (1.5, 2.0)
    init_db()
    print(f"Simulating {farmers} farmers over {area[0]} x {area[1]} degrees (~{area[0] * 111:.0f} x {area[1] * 95:.0f} km), "
          "7 screens x 2 visits, 70% Hindi, fake upstreams, caches off vs on...\n")
    before, t_before, n = run(farmers, cached=False, area=area)
    after, t_after, _ = run(farmers, cached=True, area=area)
    keys = ["Gemini", "Earth Engine observed()", "Open-Meteo history (6 HTTP calls each)", "Open-Meteo forecast",
            "SoilGrids", "Translation API requests", "Translation API characters"]
    print(f"{'upstream calls':42s}{'caches off':>14s}{'caches on':>12s}{'reduction':>12s}")
    for k in keys:
        b, a = before[k], after[k]
        red = f"{b / a:,.0f}x" if a else "all"
        print(f"{k:42s}{b:>14,}{a:>12,}{red:>12s}")
    print(f"\n{n:,} requests served. In-process time: {t_before:.1f}s (off) vs {t_after:.1f}s (on)")
    print("(Translation characters are billed; the fake Gemini text is unique per call and about 2,000 characters,"
          " like the real thing.)")
    print("cache stats:", {k: v for k, v in cache.stats().items() if v["hits"] or v["misses"]})
    if check:
        sys.exit(0 if check_limits(after, farmers) else 1)


# The most upstream calls PER FARMER that the shipped code may make in this scenario (7 screens, 2 visits). These are
# the current measurements with headroom; a change that pushes a number above its limit is making more paid or rate
# limited calls than before and needs a reason. Lower a limit when an optimisation lands.
# Scenario for the recorded values: `simulate_sessions.py 150 0.3` (150 farmers in one ~33 km square). The run is
# seeded, so the numbers repeat exactly; the headroom is for legitimate small changes.
LIMITS_PER_FARMER = {
    "Gemini": 2.0,  # measured 1.61
    "Earth Engine observed()": 1.25,  # measured 1.00: one per plot per day, however many screens are opened
    "Open-Meteo history (6 HTTP calls each)": 0.4,  # measured 0.29: shared by farmers in the same ~5 km cell
    "Open-Meteo forecast": 0.4,  # measured 0.29
    "SoilGrids": 1.25,  # measured 0.99
    "Translation API characters": 3300,  # measured 2,593
}


def check_limits(after: Counter, farmers: int) -> bool:
    print(f"\n{'check (per farmer)':42s}{'measured':>12s}{'limit':>10s}")
    ok = True
    for key, limit in LIMITS_PER_FARMER.items():
        per = after[key] / farmers
        passed = per <= limit
        ok &= passed
        print(f"{key:42s}{per:>12.2f}{limit:>10}   {'ok' if passed else 'TOO MANY CALLS'}")
    print("\nRESULT:", "PASS" if ok else "FAIL: upstream calls per farmer went up")
    return ok


if __name__ == "__main__":
    main()
