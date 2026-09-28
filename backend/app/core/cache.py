"""Small in-process cache: bounded LRU, per-entry expiry, and single-flight computation.

Most of what this API fetches (satellite/weather history, soil grids, AI advice) is identical for every farmer in
the same area. Single-flight means that when many requests ask for the same missing key at once, for example
thousands of farmers in one 5 km cell at 6 am, only the first computes it and the rest wait for and share its
result. Failures are never cached, so a transient upstream error is retried by the next request.

This is the seam for a shared cache (Redis / Memorystore): a fleet of instances would swap this implementation
and keep the `get_or_compute()` call sites unchanged. Until then each instance has its own cache.
"""

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_now = time.monotonic  # patched in tests
_NEGATIVE = object()  # stored for a short time when a lookup found nothing, so a dead upstream is not hammered
_REGISTRY: list["TTLCache"] = []
_WAIT_FOR_LEADER_S = 60.0


class _Flight:
    """One in-progress computation that other callers can wait on."""

    def __init__(self) -> None:
        self.done = threading.Event()


class TTLCache:
    def __init__(self, name: str, max_entries: int = 10_000, default_ttl: float = 3600.0) -> None:
        self.name = name
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()  # key -> (expires_at, value)
        self._inflight: dict[Any, _Flight] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        _REGISTRY.append(self)

    # -- basic operations
    def get(self, key: Any) -> tuple[bool, Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False, None
            expires_at, value = entry
            if expires_at <= _now():
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, value

    def set(self, key: Any, value: Any, ttl: float | None = None) -> None:
        with self._lock:
            self._data[key] = (_now() + (self.default_ttl if ttl is None else ttl), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)  # least recently used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._inflight.clear()
            self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    # -- the main entry point
    def get_or_compute(self, key: Any, compute: Callable[[], Any], ttl: float | None = None,
                       cache_if: Callable[[Any], bool] = lambda v: v is not None,
                       negative_ttl: float | None = None) -> Any:
        """Return the cached value, or run `compute()` once even if many callers ask at the same time.

        Values for which `cache_if(value)` is false (default: None) are returned but not stored, so "no data"
        results are recomputed next time, unless `negative_ttl` is given: then that outcome is remembered for
        that many seconds (use it for upstreams that may be down). Exceptions propagate and are never cached.
        """
        if not get_settings().cache_enabled:
            return compute()
        deadline = _now() + _WAIT_FOR_LEADER_S
        while True:
            hit, value = self.get(key)
            if hit:
                self.hits += 1
                return None if value is _NEGATIVE else value
            with self._lock:
                flight = self._inflight.get(key)
                leader = flight is None
                if leader:
                    flight = self._inflight[key] = _Flight()
            if leader:
                self.misses += 1
                try:
                    value = compute()
                    if cache_if(value):
                        self.set(key, value, ttl)
                    elif negative_ttl:
                        self.set(key, _NEGATIVE, negative_ttl)
                    return value
                finally:
                    with self._lock:
                        self._inflight.pop(key, None)
                    flight.done.set()
            # Follower: wait for the leader, then look again. If the leader failed or the value was not
            # cacheable, the loop makes this caller the next leader.
            flight.done.wait(timeout=max(0.0, deadline - _now()))
            if _now() >= deadline:
                return compute()  # leader is stuck; do not wait forever


def clear_all_caches() -> None:
    """Empty every cache (used by tests, and available for operational cache flushes)."""
    for c in _REGISTRY:
        c.clear()


def stats() -> dict[str, dict[str, int]]:
    return {c.name: {"entries": len(c), "hits": c.hits, "misses": c.misses} for c in _REGISTRY}
