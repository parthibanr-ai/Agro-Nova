"""Optional shared store (Redis / Memorystore) behind the in-process caches and rate limits.

Without REDIS_URL nothing here is used and each instance works alone, exactly as before. With it:

  * caches marked `shared=True` look in Redis when their own memory misses, and publish what they compute, so the
    second instance to need "weather for this cell today" reads it instead of asking Open-Meteo again, and a new
    instance does not start cold;
  * a per-key lock stops several instances computing the same missing value at once (the cross-instance version of
    the single-flight in core/cache.py);
  * rate-limit counters are global, so a farmer's 20 requests a minute stay 20 however many instances there are.

Redis is an accelerator, never a dependency: every call has a short timeout, any error falls back to the local
behaviour, and after an error the store is skipped for a few seconds so a dead Redis does not add its timeout to
every request. Memorystore for Redis (a single node, no cluster mode) is assumed: the rate-limit script touches
several keys at once.

Values are stored as JSON (never pickle), so a compromised Redis cannot make the API run code. Types that go
through it are registered with `@shareable`.
"""

import dataclasses
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from app.core import metrics
from app.core.config import get_settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "agrin:v1:"

_TYPES: dict[str, type] = {}


def shareable(cls: type) -> type:
    """Allow a dataclass to be stored in the shared cache."""
    _TYPES[cls.__name__] = cls
    return cls


class NotShareable(TypeError):
    """The value holds something this store does not know how to write as JSON."""


def _enc(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, datetime):
        return {"__datetime": v.isoformat()}
    if isinstance(v, date):
        return {"__date": v.isoformat()}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        if type(v).__name__ not in _TYPES:
            raise NotShareable(type(v).__name__)
        return {"__dc": type(v).__name__, **{f.name: _enc(getattr(v, f.name)) for f in dataclasses.fields(v)}}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise NotShareable("dict with non-string keys")
        return {k: _enc(x) for k, x in v.items()}
    raise NotShareable(type(v).__name__)


def _dec(v: Any) -> Any:
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        if "__date" in v:
            return date.fromisoformat(v["__date"])
        if "__datetime" in v:
            return datetime.fromisoformat(v["__datetime"])
        if "__dc" in v:
            cls = _TYPES[v["__dc"]]
            return cls(**{k: _dec(x) for k, x in v.items() if k != "__dc"})
        return {k: _dec(x) for k, x in v.items()}
    return v


def dumps(value: Any) -> str:
    return json.dumps(_enc(value), separators=(",", ":"), ensure_ascii=False)


def loads(data: str | bytes) -> Any:
    return _dec(json.loads(data))


# Atomically: if any counter is already at its limit, change nothing and say which one; otherwise count the request
# against all of them. (A refused request must not use up the daily allowance.) ARGV = limit, ttl for each key.
_RATE_LIMIT_LUA = """
for i, k in ipairs(KEYS) do
  local c = tonumber(redis.call('GET', k) or '0')
  if c >= tonumber(ARGV[2 * i - 1]) then return i end
end
for i, k in ipairs(KEYS) do
  if redis.call('INCR', k) == 1 then redis.call('EXPIRE', k, tonumber(ARGV[2 * i])) end
end
return 0
"""


class StoreUnavailable(RuntimeError):
    """Redis did not answer in time, or refused; the caller falls back to local behaviour."""


class RedisStore:
    def __init__(self, client) -> None:
        self._r = client
        self._rate_limit = client.register_script(_RATE_LIMIT_LUA)
        self._down_until = 0.0
        self._lock = threading.Lock()

    # -- health
    @property
    def usable(self) -> bool:
        return time.monotonic() >= self._down_until

    def _call(self, fn: Callable[[], Any]) -> Any:
        if not self.usable:
            raise StoreUnavailable("skipped: recent failure")
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - timeouts, refused connections, READONLY failovers...
            with self._lock:
                already_down = self._down_until > time.monotonic()
                self._down_until = time.monotonic() + get_settings().redis_down_backoff_s
            metrics.SHARED_STORE_ERRORS.inc()
            if not already_down:
                logger.warning("shared store unavailable, running on local memory for %ss: %s",
                               get_settings().redis_down_backoff_s, exc)
            raise StoreUnavailable(str(exc)) from exc

    # -- values
    def get(self, key: str) -> str | None:
        return self._call(lambda: self._r.get(KEY_PREFIX + key))

    def get_many(self, keys: list[str]) -> list[str | None]:
        if not keys:
            return []
        return self._call(lambda: self._r.mget([KEY_PREFIX + k for k in keys]))

    def set(self, key: str, data: str, ttl_s: float) -> None:
        self._call(lambda: self._r.set(KEY_PREFIX + key, data, px=max(1, int(ttl_s * 1000))))

    def set_many(self, items: list[tuple[str, str]], ttl_s: float) -> None:
        if not items:
            return

        def write():
            pipe = self._r.pipeline(transaction=False)
            for k, data in items:
                pipe.set(KEY_PREFIX + k, data, px=max(1, int(ttl_s * 1000)))
            pipe.execute()

        self._call(write)

    def ttl_s(self, key: str) -> float | None:
        ms = self._call(lambda: self._r.pttl(KEY_PREFIX + key))
        return ms / 1000 if ms is not None and ms > 0 else None

    # -- cross-instance single flight
    def try_lock(self, key: str, ttl_s: float) -> bool:
        return bool(self._call(lambda: self._r.set(KEY_PREFIX + "lock:" + key, "1", nx=True,
                                                    px=max(1, int(ttl_s * 1000)))))

    def unlock(self, key: str) -> None:
        self._call(lambda: self._r.delete(KEY_PREFIX + "lock:" + key))

    def is_locked(self, key: str) -> bool:
        return bool(self._call(lambda: self._r.exists(KEY_PREFIX + "lock:" + key)))

    # -- rate limits
    def rate_limit(self, rules: list[tuple[str, int, int]]) -> int | None:
        """rules: (counter key, limit, window seconds). Returns None if allowed (and counted), else the index of the
        first rule already at its limit."""
        keys = [KEY_PREFIX + k for k, _, _ in rules]
        args: list[int] = []
        for _, limit, window in rules:
            args += [limit, window + 1]
        hit = self._call(lambda: self._rate_limit(keys=keys, args=args))
        return None if not hit else int(hit) - 1

    def ping(self) -> bool:
        try:
            return bool(self._call(self._r.ping))
        except StoreUnavailable:
            return False


_store: RedisStore | None = None
_store_url: str | None = None
_store_lock = threading.Lock()


def get_store() -> RedisStore | None:
    """The shared store, or None when REDIS_URL is not set or the store is in its post-error backoff."""
    global _store, _store_url
    s = get_settings()
    if not s.redis_url:
        return None
    with _store_lock:
        if _store is None or _store_url != s.redis_url:
            import redis

            client = redis.Redis.from_url(
                s.redis_url, decode_responses=True, socket_timeout=s.redis_socket_timeout_s,
                socket_connect_timeout=s.redis_socket_timeout_s, health_check_interval=30,
                max_connections=s.redis_max_connections)
            _store, _store_url = RedisStore(client), s.redis_url
        store = _store
    return store if store.usable else None


def use_store(store: RedisStore | None) -> None:
    """Tests: install a store directly (or None to go back to config)."""
    global _store, _store_url
    with _store_lock:
        _store = store
        _store_url = get_settings().redis_url if store is not None else None


def is_configured() -> bool:
    return bool(get_settings().redis_url)
