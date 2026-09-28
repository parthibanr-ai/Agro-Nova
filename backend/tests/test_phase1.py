"""Phase 1: caching and per-farmer limits."""

import io
from datetime import date, timedelta

from app.core import cache, ratelimit
from app.providers.base import DailyForecast, ObservedClimate
from app.services import advice_cache
from app.services.context import soil_summary
from tests.conftest import NASHIK

CORNERS = [(30.9010, 75.8570), (30.9010, 75.8590), (30.9030, 75.8590), (30.9030, 75.8570)]


def _create(client, **over):
    body = {"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab", "corners": NASHIK, **over}
    return client.post("/api/v1/plots", json=body).json()["id"]


# ------------------------------------------------------------------ soil reaches the prompt
def test_farmer_entered_soil_values_reach_the_prompt():
    text = soil_summary({"ph": 7.6, "oc": 0.61, "n": 210, "p": 14, "k": 260, "ec": 0.4})
    assert "pH: 7.6" in text and "Organic carbon: 0.61 %" in text and "Available nitrogen: 210 kg/ha" in text
    assert "No soil data" not in text


def test_soilgrids_shape_still_works():
    assert soil_summary({"phh2o": {"label": "pH (water)", "value": 8.6, "unit": ""}}) == "pH (water): 8.6"
    assert soil_summary({}) == "No soil data available."


# ------------------------------------------------------------------ bucketing
def test_similar_conditions_round_to_the_same_bucket():
    a = ObservedClimate(30, rain_mm=38.2, rain_normal_mm=134.9, tmean_c=29.8, ndvi=0.012, soil_moisture_pct=11.6)
    b = ObservedClimate(30, rain_mm=39.9, rain_normal_mm=132.0, tmean_c=30.2, ndvi=0.02, soil_moisture_pct=12.4)
    assert advice_cache.bucket_observed(a) == advice_cache.bucket_observed(b)
    far = ObservedClimate(30, rain_mm=120, rain_normal_mm=135, tmean_c=29.8, ndvi=0.012, soil_moisture_pct=11.6)
    assert advice_cache.bucket_observed(a) != advice_cache.bucket_observed(far)


def test_bucketing_never_turns_a_small_normal_into_zero():
    assert advice_cache.bucket_observed(ObservedClimate(30, rain_mm=1, rain_normal_mm=2)).rain_normal_mm == 5


def test_area_and_soil_buckets():
    assert advice_cache.bucket_area_ha(2.99) == 3.0 and advice_cache.bucket_area_ha(0.1) == 0.5
    assert advice_cache.bucket_soil({"ph": 7.63, "n": 213}) == {"ph": 7.6, "n": 210.0}


# ------------------------------------------------------------------ AI advice is shared between farmers
def test_farmers_with_the_same_conditions_share_one_gemini_call(client, monkeypatch):
    from app.services import crop_recommendation

    calls = []

    def fake_gemini(prompt):
        calls.append(prompt)
        return {"target_season": {"local_name": "Rabi"}, "recommendations": [], "basis_summary": "ok"}

    monkeypatch.setattr(crop_recommendation, "call_gemini", fake_gemini)
    codes = []
    for uid in ("farmer-a", "farmer-b", "farmer-c"):  # three different farmers, identical plots
        h = {"X-Dev-User": uid}
        body = {"name": "x", "crop": "wheat", "country": "IN", "state": "Punjab", "corners": NASHIK}
        pid = client.post("/api/v1/plots", json=body, headers=h).json()["id"]
        codes.append(client.get(f"/api/v1/plots/{pid}/crop-recommendation", headers=h).status_code)
    assert codes == [200, 200, 200]
    assert len(calls) == 1, "three farmers with identical conditions must cost one Gemini call"


def test_different_crop_or_state_gets_its_own_answer(client, monkeypatch):
    from app.services import crop_recommendation

    calls = []
    monkeypatch.setattr(crop_recommendation, "call_gemini",
                        lambda prompt: calls.append(prompt) or {"target_season": {}, "recommendations": []})
    for crop, state in (("wheat", "Punjab"), ("rice", "Punjab"), ("wheat", "Haryana")):
        pid = _create(client, crop=crop, state=state)
        assert client.get(f"/api/v1/plots/{pid}/crop-recommendation").status_code == 200
    assert len(calls) == 3


def test_gemini_failure_is_not_cached(client, monkeypatch):
    from app.services import crop_recommendation

    state = {"n": 0}

    def flaky(prompt):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("503 overloaded")
        return {"target_season": {}, "recommendations": []}

    monkeypatch.setattr(crop_recommendation, "call_gemini", flaky)
    pid = _create(client)
    assert client.get(f"/api/v1/plots/{pid}/crop-recommendation").status_code == 502
    assert client.get(f"/api/v1/plots/{pid}/crop-recommendation").status_code == 200


def test_cached_answers_are_independent_copies(client, monkeypatch):
    from app.services import crop_recommendation

    monkeypatch.setattr(crop_recommendation, "call_gemini",
                        lambda prompt: {"target_season": {"local_name": "Rabi"}, "recommendations": []})
    pid = _create(client)
    first = client.get(f"/api/v1/plots/{pid}/crop-recommendation").json()
    first["target_season"]["local_name"] = "tampered"
    assert client.get(f"/api/v1/plots/{pid}/crop-recommendation").json()["target_season"]["local_name"] == "Rabi"


# ------------------------------------------------------------------ climate lookups are shared per cell
class _Counting:
    def __init__(self):
        self.observed_calls = self.forecast_calls = 0

    def observed(self, corners, window_days):
        self.observed_calls += 1
        return ObservedClimate(window_days, rain_mm=38, rain_normal_mm=135, tmean_c=30,
                               sources=["Open-Meteo archive (ERA5)"])

    def forecast(self, lat, lon, days):
        self.forecast_calls += 1
        return [DailyForecast(date.today() + timedelta(days=i), 35, 22, 0.0, 5.0) for i in range(days)]


def _provider(monkeypatch, counting):
    from app.providers import registry

    monkeypatch.setattr(registry, "is_earth_engine_ready", lambda: False)
    p = registry.ResilientProvider()
    p._fallback = counting
    return p


def test_farms_in_the_same_cell_share_one_weather_lookup(monkeypatch):
    counting = _Counting()
    p = _provider(monkeypatch, counting)
    p.observed([(30.9010, 75.8570), (30.9012, 75.8572)], 30)  # same ~5 km cell
    p.observed([(30.9040, 75.8600), (30.9042, 75.8602)], 30)
    assert counting.observed_calls == 1
    p.observed([(31.20, 75.85), (31.2002, 75.8502)], 30)  # 33 km away: a different cell
    assert counting.observed_calls == 2


def test_forecasts_are_shared_per_cell_and_callers_get_copies(monkeypatch):
    counting = _Counting()
    p = _provider(monkeypatch, counting)
    a = p.forecast(30.9010, 75.8570, 10)
    b = p.forecast(30.9040, 75.8600, 10)
    assert counting.forecast_calls == 1
    a.clear()
    assert len(b) == 10 and len(p.forecast(30.9010, 75.8570, 10)) == 10


def test_a_failing_earth_engine_is_not_retried_by_every_request(monkeypatch):
    from app.providers import registry

    p = _provider(monkeypatch, _Counting())
    monkeypatch.setattr(registry, "is_earth_engine_ready", lambda: True)
    calls = []

    class DownEE:
        def observed(self, corners, window_days):
            calls.append(1)
            raise RuntimeError("EE down")

    p._gee = DownEE()
    for _ in range(5):
        assert p.observed(CORNERS, 30).rain_mm == 38  # served from the fallback each time
    assert len(calls) == 1


def test_repeat_visits_to_the_forecast_screen_make_no_new_upstream_calls(client, monkeypatch):
    from app.api import routes

    counting = _Counting()
    provider = _provider(monkeypatch, counting)
    monkeypatch.setattr(routes, "get_climate_provider", lambda: provider)
    pid = _create(client)
    for _ in range(3):
        assert client.get(f"/api/v1/plots/{pid}/forecast").status_code == 200
    assert (counting.observed_calls, counting.forecast_calls) == (1, 1)


# ------------------------------------------------------------------ soil grid and geocoding caches
def test_soilgrids_is_cached_per_cell_and_failures_are_remembered_briefly(monkeypatch):
    from app.services import soil

    calls = []
    monkeypatch.setattr(soil, "_fetch_soilgrids", lambda lat, lon: calls.append((lat, lon)) or {"phh2o": {}})
    soil.fetch_soilgrids(30.9010, 75.8570)
    soil.fetch_soilgrids(30.9011, 75.8571)  # ~15 m away
    assert len(calls) == 1

    fails = []
    monkeypatch.setattr(soil, "_fetch_soilgrids", lambda lat, lon: fails.append(1))  # returns None: ISRIC is down
    for _ in range(4):
        assert soil.fetch_soilgrids(12.0, 77.0) is None
    assert len(fails) == 1


def test_repeat_place_searches_do_not_hit_nominatim_again(monkeypatch):
    from app.services import geocode

    calls = []
    monkeypatch.setattr(geocode, "_search",
                        lambda q, limit: calls.append(q) or [{"display_name": "X", "lat": 1, "lon": 2}])
    for q in ("Nashik", "  nashik  ", "NASHIK"):
        assert geocode.search(q)
    assert len(calls) == 1


# ------------------------------------------------------------------ translation cache is bounded
def test_translation_cache_is_bounded_and_reused(monkeypatch):
    from app.services import translation

    small = cache.TTLCache("t-small", max_entries=3)
    monkeypatch.setattr(translation, "_CACHE", small)
    monkeypatch.setattr(translation, "get_settings", lambda: type("S", (), {"translate_api_key": "k"})())
    sent = []

    class FakeResp:
        def __init__(self, chunk):
            self.chunk = chunk

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"translations": [{"translatedText": f"<{t}>"} for t in self.chunk]}}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, params, json):
            sent.extend(json["q"])
            return FakeResp(json["q"])

    monkeypatch.setattr(translation.httpx, "Client", FakeClient)
    translation.translate_texts(["a", "b"], "hi")
    translation.translate_texts(["a", "b"], "hi")
    assert sent == ["a", "b"], "the second call must be served from the cache"
    translation.translate_texts([f"s{i}" for i in range(10)], "hi")
    assert len(small) <= 3, "the cache must not grow without bound"


# ------------------------------------------------------------------ rate limits
def test_ai_endpoint_is_rate_limited_per_farmer_with_retry_after(client, monkeypatch):
    from app.core.config import get_settings
    from app.services import crop_recommendation

    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 3)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: {"target_season": {}, "recommendations": []})
    pid = _create(client)
    url = f"/api/v1/plots/{pid}/crop-recommendation"
    codes = [client.get(url).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert int(client.get(url).headers["Retry-After"]) >= 1
    assert client.get("/api/v1/resilience", headers={"X-Dev-User": "someone-else"}).status_code == 200


def test_limits_reset_after_the_window(client, monkeypatch):
    from app.core.config import get_settings
    from app.services import crop_recommendation

    clock = [100.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 1)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: {"target_season": {}, "recommendations": []})
    pid = _create(client)
    url = f"/api/v1/plots/{pid}/crop-recommendation"
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 429
    clock[0] += 61
    assert client.get(url).status_code == 200


def test_diagnosis_has_its_own_stricter_limit(client, monkeypatch):
    from app.core.config import get_settings
    from app.services import diagnosis

    monkeypatch.setattr(get_settings(), "diagnosis_rate_per_minute", 2)
    monkeypatch.setattr(diagnosis, "diagnose",
                        lambda *a, **k: diagnosis.assemble({"status": "healthy", "confidence": 0.9}))
    codes = []
    for _ in range(4):
        files = {"image": ("leaf.jpg", io.BytesIO(b"\xff\xd8x"), "image/jpeg")}
        codes.append(client.post("/api/v1/diagnosis", files=files).status_code)
    assert codes == [200, 200, 429, 429]


def test_limits_can_be_switched_off(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 1)
    assert [client.get("/api/v1/resilience").status_code for _ in range(4)] == [200] * 4


def test_the_rate_limit_table_does_not_grow_forever(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: clock[0])
    monkeypatch.setattr(ratelimit, "_calls", 0)  # the prune runs every N calls; start counting from a known point
    for i in range(ratelimit._PRUNE_EVERY):
        ratelimit._hit("ai-min", f"user-{i}", 5, 60)
        if i == ratelimit._PRUNE_EVERY - 2:
            clock[0] += 120  # every earlier window has expired by the time the prune runs
    assert len(ratelimit._windows) < 10


def test_cache_can_be_disabled(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "cache_enabled", False)
    c = cache.TTLCache("off")
    calls = []
    for _ in range(3):
        c.get_or_compute("k", lambda: calls.append(1) or "v")
    assert len(calls) == 3
