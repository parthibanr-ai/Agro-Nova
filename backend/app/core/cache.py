"""Small in-process cache: bounded LRU, per-entry expiry, and single-flight computation.

Most of what this API fetches (satellite/weather history, soil grids, AI advice) is identical for every farmer in
the same area. Single-flight means that when many requests ask for the same missing key at once, for example
thousands of farmers in one 5 km cell at 6 am, only the first computes it and the rest wait for and share its
result. Failures are never cached, so a transient upstream error is retried by the next request.

With REDIS_URL set, a cache created with `shared=True` also reads and writes Redis (core/shared_store.py): what one
instance computes, the others read, and a lock makes only one instance compute a missing value at a time. Without it
each instance has its own cache, as before.
"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from app.core import metrics, shared_store
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
    def __init__(self, name: str, max_entries: int = 10_000, default_ttl: float = 3600.0,
                 shared: bool = False) -> None:
        self.name = name
        self.shared = shared  # also use Redis, when configured
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

    # -- the shared (Redis) layer; every method quietly does nothing when Redis is off or failing
    def _shared_key(self, key: Any) -> str:
        return f"cache:{self.name}:{hashlib.sha1(repr(key).encode()).hexdigest()}"

    def _store(self):
        return shared_store.get_store() if self.shared and get_settings().cache_enabled else None

    def shared_get(self, key: Any) -> tuple[bool, Any, float | None]:
        """(found, value, seconds left) from Redis."""
        store = self._store()
        if store is None:
            return False, None, None
        try:
            raw = store.get(self._shared_key(key))
            if raw is None:
                metrics.SHARED_CACHE.labels(self.name, "miss").inc()
                return False, None, None
            metrics.SHARED_CACHE.labels(self.name, "hit").inc()
            return True, shared_store.loads(raw), store.ttl_s(self._shared_key(key))
        except shared_store.StoreUnavailable:
            return False, None, None
        except Exception:  # noqa: BLE001 - an entry written by an older schema: treat it as missing
            logger.warning("unreadable shared cache entry in %s", self.name, exc_info=True)
            return False, None, None

    def shared_set(self, key: Any, value: Any, ttl: float) -> None:
        store = self._store()
        if store is None:
            return
        try:
            store.set(self._shared_key(key), shared_store.dumps(value), ttl)
        except (shared_store.StoreUnavailable, shared_store.NotShareable):
            pass

    def shared_get_many(self, keys: list[Any]) -> dict[Any, Any]:
        """Batch lookup for caches read in bulk (translations): {key: value} for the keys Redis has."""
        store = self._store()
        if store is None or not keys:
            return {}
        try:
            raws = store.get_many([self._shared_key(k) for k in keys])
            found = {k: shared_store.loads(r) for k, r in zip(keys, raws, strict=True) if r is not None}
        except shared_store.StoreUnavailable:
            return {}
        metrics.SHARED_CACHE.labels(self.name, "hit").inc(len(found))
        metrics.SHARED_CACHE.labels(self.name, "miss").inc(len(keys) - len(found))
        return found

    def shared_set_many(self, items: dict[Any, Any], ttl: float) -> None:
        store = self._store()
        if store is None or not items:
            return
        try:
            store.set_many([(self._shared_key(k), shared_store.dumps(v)) for k, v in items.items()], ttl)
        except (shared_store.StoreUnavailable, shared_store.NotShareable):
            pass

    def _adopt_shared(self, key: Any, ttl: float) -> tuple[bool, Any]:
        found, value, left = self.shared_get(key)
        if found:
            self.set(key, value, min(ttl, left) if left else ttl)  # never outlive the copy it came from
        return found, value

    def _compute_shared(self, key: Any, ttl: float) -> tuple[bool, Any, bool]:
        """Before computing, see whether another instance has (or is about to have) the answer.

        Returns (supplied, value, holds_lock). When Redis supplied the value the caller is done; otherwise it computes
        and, if `holds_lock`, must release the cross-instance lock afterwards.
        """
        store = self._store()
        if store is None:
            return False, None, False
        found, value = self._adopt_shared(key, ttl)
        if found:
            return True, value, False
        skey = self._shared_key(key)
        try:
            if store.try_lock(skey, get_settings().shared_lock_ttl_s):
                return False, None, True
            # Another instance is computing it: wait for its result instead of repeating the upstream call.
            deadline = _now() + get_settings().shared_lock_wait_s
            while _now() < deadline and store.is_locked(skey):
                time.sleep(0.2)
            found, value = self._adopt_shared(key, ttl)
            return found, value, False
        except shared_store.StoreUnavailable:
            return False, None, False

    def _release_shared_lock(self, key: Any) -> None:
        store = self._store()
        if store is not None:
            try:
                store.unlock(self._shared_key(key))
            except shared_store.StoreUnavailable:
                pass

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
                effective_ttl = self.default_ttl if ttl is None else ttl
                holds_lock = False
                try:
                    supplied, value, holds_lock = self._compute_shared(key, effective_ttl)
                    if supplied:
                        self.hits += 1  # another instance did the work
                        return value
                    self.misses += 1
                    value = compute()
                    if cache_if(value):
                        self.set(key, value, ttl)
                        self.shared_set(key, value, effective_ttl)
                    elif negative_ttl:
                        self.set(key, _NEGATIVE, negative_ttl)
                    return value
                finally:
                    if holds_lock:
                        self._release_shared_lock(key)
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
