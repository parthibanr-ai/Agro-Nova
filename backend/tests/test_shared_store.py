"""The optional Redis layer: shared caches, cross-instance single flight, global rate limits, and falling back to
local memory when Redis fails.

Runs against fakeredis (with Lua) by default, or a real Redis when TEST_REDIS_URL is set. Two "instances" are two
cache objects with the same name and separate memory, talking to the same Redis.
"""

import os
import threading
import time
from datetime import date

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from app.core import cache, ratelimit, shared_store
from app.core.cache import TTLCache
from app.core.config import get_settings
from app.main import app
from app.providers.base import DailyForecast, ObservedClimate
from app.services import translation
from tests.conftest import NASHIK, FakeProvider


@pytest.fixture()
def store(monkeypatch):
    url = os.environ.get("TEST_REDIS_URL")
    if url:
        client = redis.Redis.from_url(url, decode_responses=True)
        client.flushdb()
    else:
        client = fakeredis.FakeRedis(decode_responses=True)
    s = shared_store.RedisStore(client)
    monkeypatch.setattr(get_settings(), "redis_url", url or "redis://fake:6379/0")
    shared_store.use_store(s)
    yield s
    shared_store.use_store(None)
    client.flushdb()


def _instance(name="probe", **kw):
    """A fresh instance: same Redis, empty local memory. (Registered caches are cleared by the autouse fixture.)"""
    return TTLCache(name, shared=True, **kw)


class _Broken:
    """A Redis that always errors, as when it is down or unreachable."""

    calls = 0

    def __getattr__(self, name):
        def fail(*a, **k):
            _Broken.calls += 1
            raise redis.exceptions.ConnectionError("connection refused")

        return fail


def _break(store):
    _Broken.calls = 0
    store._r = _Broken()
    store._rate_limit = _Broken().script


# ------------------------------------------------------------------ what can be stored
def test_climate_objects_round_trip_through_json():
    observed = ObservedClimate(30, rain_mm=12.5, rain_normal_mm=80, tmean_c=29.1, as_of=date(2026, 9, 1),
                               sources=["CHIRPS", "SMAP"])
    forecast = [DailyForecast(date(2026, 9, 28), 34.0, 22.0, 0.0, 5.5), DailyForecast(date(2026, 9, 29), 33, 21, 4.0)]
    payload = {"obs": observed, "days": forecast, "text": "हिन्दी", "n": [1, 2.5, None, True]}
    assert shared_store.loads(shared_store.dumps(payload)) == payload


def test_unknown_types_are_refused_rather_than_pickled():
    with pytest.raises(shared_store.NotShareable):
        shared_store.dumps({"x": object()})
    with pytest.raises(shared_store.NotShareable):
        shared_store.dumps({1: "non-string key"})


# ------------------------------------------------------------------ shared caches
def test_a_second_instance_reads_what_the_first_computed(store):
    a, b = _instance(), _instance()
    calls = []
    assert a.get_or_compute(("cell", 1), lambda: calls.append("a") or {"rain": 12}, ttl=600) == {"rain": 12}
    assert b.get_or_compute(("cell", 1), lambda: calls.append("b") or {"rain": 99}, ttl=600) == {"rain": 12}
    assert calls == ["a"]
    assert b.hits == 1 and b.misses == 0  # counted as a hit: no upstream work was done


def test_the_copy_taken_from_redis_never_outlives_the_original(store):
    a, b = _instance(), _instance()
    a.get_or_compute("k", lambda: "v", ttl=100)
    assert store.ttl_s(a._shared_key("k")) <= 100
    b.get_or_compute("k", lambda: "other")
    remaining = b._data["k"][0] - time.monotonic()
    assert 0 < remaining <= 100


def test_only_caches_marked_shared_use_redis(store):
    a, b = TTLCache("private", shared=False), TTLCache("private", shared=False)
    a.get_or_compute("k", lambda: 1)
    assert b.get_or_compute("k", lambda: 2) == 2


def test_failures_and_no_data_are_not_shared(store):
    a, b = _instance(), _instance()
    a.get_or_compute("nothing", lambda: None, negative_ttl=60)  # remembered locally only
    assert b.get_or_compute("nothing", lambda: "found now") == "found now"
    with pytest.raises(RuntimeError):
        a.get_or_compute("boom", lambda: (_ for _ in ()).throw(RuntimeError("upstream down")))
    assert b.get_or_compute("boom", lambda: "recovered") == "recovered"


def test_instances_racing_for_the_same_missing_value_do_one_upstream_call(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "shared_lock_wait_s", 5)
    upstream = []

    def slow():
        upstream.append(threading.current_thread().name)
        time.sleep(0.6)
        return {"ndvi": 0.4}

    results = []
    instances = [_instance() for _ in range(4)]
    threads = [threading.Thread(target=lambda c=c: results.append(c.get_or_compute("plot-9", slow, ttl=600)))
               for c in instances]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(upstream) == 1, upstream
    assert results == [{"ndvi": 0.4}] * 4
    assert not store.is_locked(instances[0]._shared_key("plot-9"))  # the lock is released


def test_a_stuck_lock_holder_is_not_waited_for_forever(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "shared_lock_wait_s", 0.5)
    a = _instance()
    assert store.try_lock(a._shared_key("k"), 60)  # some other instance took it and then hung
    started = time.monotonic()
    assert a.get_or_compute("k", lambda: "computed anyway") == "computed anyway"
    assert time.monotonic() - started < 3
    assert store.is_locked(a._shared_key("k"))  # and we did not delete a lock that was not ours


def test_a_failed_leader_releases_the_lock_for_the_next_instance(store):
    a, b = _instance(), _instance()
    with pytest.raises(RuntimeError):
        a.get_or_compute("k", lambda: (_ for _ in ()).throw(RuntimeError("upstream down")))
    assert b.get_or_compute("k", lambda: "ok") == "ok"


def test_the_real_registry_caches_survive_a_round_trip(store):
    from app.providers import registry

    key = ("om", 30.05, 74.55, 30, date.today())
    obs = ObservedClimate(30, rain_mm=5, sources=["Open-Meteo"], as_of=date.today())
    assert registry._weather_cache.get_or_compute(key, lambda: obs, ttl=600) == obs
    registry._weather_cache.clear()  # a different instance: empty memory
    assert registry._weather_cache.get_or_compute(key, lambda: 1 / 0, ttl=600) == obs


# ------------------------------------------------------------------ Redis failing must never break a request
def test_redis_errors_fall_back_to_local_and_are_skipped_for_a_while(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_down_backoff_s", 30)
    _break(store)
    c = _instance()
    assert c.get_or_compute("a", lambda: 1) == 1  # works with a dead Redis
    calls_after_first = _Broken.calls
    assert calls_after_first >= 1
    assert c.get_or_compute("b", lambda: 2) == 2
    assert _Broken.calls == calls_after_first  # in backoff: Redis is not even tried
    assert shared_store.get_store() is None  # so the metrics gauge reads 0
    assert c.get_or_compute("a", lambda: 99) == 1  # local memory still works


def test_redis_recovers_after_the_backoff(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_down_backoff_s", 0)
    good_client, good_script = store._r, store._rate_limit
    _break(store)
    c = _instance()
    c.get_or_compute("a", lambda: 1)
    store._r, store._rate_limit = good_client, good_script
    time.sleep(0.01)
    c.clear()
    d = _instance()
    d.get_or_compute("z", lambda: 5, ttl=100)
    assert _instance().get_or_compute("z", lambda: 0) == 5


# ------------------------------------------------------------------ translations
def test_translations_done_by_one_instance_are_reused_by_the_others(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "translate_api_key", "k")
    requests = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, params, json):
            requests.append(list(json["q"]))
            body = {"data": {"translations": [{"translatedText": f"hi:{t}"} for t in json["q"]]}}
            return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()

    monkeypatch.setattr(translation.httpx, "Client", FakeClient)
    monkeypatch.setattr(translation, "_CACHE", TTLCache("translation", shared=True, default_ttl=600))
    assert translation.translate_texts(["Water the crop", "Mulch"], "hi") == {
        "Water the crop": "hi:Water the crop", "Mulch": "hi:Mulch"}
    assert len(requests) == 1
    translation._CACHE.clear()  # another instance, cold memory
    out = translation.translate_texts(["Mulch", "Water the crop", "New sentence"], "hi")
    assert out["Mulch"] == "hi:Mulch" and out["New sentence"] == "hi:New sentence"
    assert requests[1] == ["New sentence"]  # only the genuinely new text is paid for


# ------------------------------------------------------------------ rate limits
H = {"X-Dev-User": "limited-farmer"}


@pytest.fixture()
def api(monkeypatch, store):
    from app.api import routes
    from app.services.enso import EnsoState

    monkeypatch.setattr(routes, "get_climate_provider", lambda: FakeProvider())
    monkeypatch.setattr(routes, "get_enso_state", lambda: EnsoState(True, "el_nino", "moderate", 1.2, "JJA 2026", "steady"))
    monkeypatch.setattr(routes.soil, "fetch_soilgrids", lambda lat, lon: {})
    client = TestClient(app, headers=H)
    pid = client.post("/api/v1/plots", json={"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab",
                                              "corners": NASHIK}).json()["id"]
    return client, pid


def test_the_limit_holds_across_instances(api, monkeypatch):
    client, pid = api
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 2)
    url = f"/api/v1/plots/{pid}/market"
    assert client.get(url).status_code == 200
    ratelimit.reset()  # a request landing on a different instance does not see this instance's memory...
    assert client.get(url).status_code == 200
    ratelimit.reset()
    r = client.get(url)  # ...but Redis has counted both
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60


def test_a_refused_request_does_not_use_up_the_daily_allowance(api, monkeypatch):
    client, pid = api
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 1)
    monkeypatch.setattr(get_settings(), "ai_rate_per_day", 3)
    url = f"/api/v1/plots/{pid}/market"
    assert client.get(url).status_code == 200
    assert [client.get(url).status_code for _ in range(5)] == [429] * 5  # a stuck client hammering the button
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)  # the next minute
    assert client.get(url).status_code == 200  # 2 of 3 daily requests used, not 7
    monkeypatch.setattr(time, "time", lambda: real_time() + 122)
    assert client.get(url).status_code == 200
    monkeypatch.setattr(time, "time", lambda: real_time() + 183)
    assert client.get(url).status_code == 429  # the third really was the last


def test_limits_fall_back_to_local_counters_when_redis_is_down(api, store, monkeypatch):
    client, pid = api
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 2)
    _break(store)
    url = f"/api/v1/plots/{pid}/market"
    assert [client.get(url).status_code for _ in range(3)] == [200, 200, 429]


def test_redis_gauge_is_exported(api, store):
    client, _ = api
    text = client.get("/metrics", headers={"X-API-Key": "change-me"}).text
    assert "agrin_shared_store_up 1.0" in text
    _break(store)
    client.get("/api/v1/resilience")  # any call that touches Redis notices it is down
    ratelimit.reset()
    cache.clear_all_caches()
    _instance().get_or_compute("x", lambda: 1)
    assert "agrin_shared_store_up 0.0" in client.get("/metrics", headers={"X-API-Key": "change-me"}).text
    assert "agrin_shared_store_errors_total" in client.get("/metrics", headers={"X-API-Key": "change-me"}).text
