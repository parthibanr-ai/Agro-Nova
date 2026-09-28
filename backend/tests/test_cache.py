import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import cache as cache_mod
from app.core.cache import TTLCache


@pytest.fixture()
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cache_mod, "_now", lambda: t[0])
    return t


def test_entries_expire(clock):
    c = TTLCache("t", default_ttl=10)
    c.set("k", 1)
    assert c.get("k") == (True, 1)
    clock[0] += 11
    assert c.get("k") == (False, None)


def test_least_recently_used_is_evicted_first(clock):
    c = TTLCache("t", max_entries=2)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")  # a is now the most recently used
    c.set("c", 3)
    assert c.get("b")[0] is False
    assert c.get("a")[0] and c.get("c")[0]


def test_get_or_compute_caches_and_counts(clock):
    c = TTLCache("t")
    calls = []
    for _ in range(3):
        assert c.get_or_compute("k", lambda: calls.append(1) or "v") == "v"
    assert len(calls) == 1 and (c.hits, c.misses) == (2, 1)


def test_none_and_failures_are_not_cached(clock):
    c = TTLCache("t")
    calls = []
    assert c.get_or_compute("none", lambda: calls.append(1)) is None
    assert c.get_or_compute("none", lambda: calls.append(1)) is None
    assert len(calls) == 2

    def boom():
        calls.append(1)
        raise RuntimeError("upstream down")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            c.get_or_compute("err", boom)
    assert len(calls) == 4


def test_many_concurrent_callers_share_one_computation():
    c = TTLCache("t")
    calls = []
    started = threading.Event()

    def slow():
        calls.append(1)
        started.set()
        time.sleep(0.3)
        return "shared"

    with ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(lambda _: c.get_or_compute("k", slow), range(30)))
    assert results == ["shared"] * 30
    assert len(calls) == 1, "single-flight: 30 simultaneous callers must trigger one upstream call"


def test_followers_take_over_when_the_leader_fails():
    c = TTLCache("t")
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            time.sleep(0.2)
            raise RuntimeError("first attempt fails")
        return "ok"

    def call(_):
        try:
            return c.get_or_compute("k", flaky)
        except RuntimeError:
            return "failed"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(call, range(6)))
    assert results.count("failed") == 1 and results.count("ok") == 5
