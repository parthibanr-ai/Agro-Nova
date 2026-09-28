"""Request limits for the endpoints that cost real money (Gemini) or hit slow upstreams.

Anonymous sign-in means anyone can mint unlimited accounts, so limits are applied per farmer *and* per IP. Counters
are fixed windows. With REDIS_URL set they live in Redis and are exact across the whole fleet (one atomic script per
request; a refused request is not counted, so being throttled by the minute limit does not eat the daily allowance).
Without Redis, or while it is failing, each instance counts in its own memory, so the effective limit across N
instances is up to N times the setting: still enough to stop a script or a stuck client from burning quota.

Cache hits are cheap but are still counted, which keeps the rule simple and predictable for farmers.
"""

import math
import threading
import time

from fastapi import Depends, HTTPException, Request

from app.core import metrics, shared_store
from app.core.auth import current_user
from app.core.config import get_settings
from app.models import User

_now = time.monotonic  # patched in tests
_lock = threading.Lock()
_windows: dict[tuple, list] = {}  # (scope, id, window_s) -> [window_start, count]
_calls = 0
_PRUNE_EVERY = 2000


def reset() -> None:
    with _lock:
        _windows.clear()


def _prune(now: float) -> None:
    expired = [k for k, (start, _) in _windows.items() if now - start >= k[2]]
    for k in expired:
        del _windows[k]


def _hit(scope: str, ident: str, limit: int, window_s: int) -> int | None:
    """Count one request. Returns None if allowed, else the number of seconds until the window resets."""
    global _calls
    now = _now()
    key = (scope, ident, window_s)
    with _lock:
        _calls += 1
        if _calls % _PRUNE_EVERY == 0:
            _prune(now)  # keeps memory bounded by the number of *active* farmers
        window = _windows.get(key)
        if window is None or now - window[0] >= window_s:
            window = _windows[key] = [now, 0]
        if window[1] >= limit:
            return max(1, math.ceil(window_s - (now - window[0])))
        window[1] += 1
        return None


def _hit_shared(rules: list[tuple[str, str, int, int]]) -> tuple[str, int] | None:
    """Count one request against every rule in Redis. Returns None if allowed, else (scope of the rule that refused,
    seconds until its window resets). Raises StoreUnavailable if Redis cannot be used."""
    store = shared_store.get_store()
    if store is None:
        raise shared_store.StoreUnavailable("no shared store")
    now = time.time()
    refused = store.rate_limit([(f"rl:{scope}:{ident}:{window}:{int(now // window)}", cap, window)
                                for scope, ident, cap, window in rules])
    if refused is None:
        return None
    scope, _, _, window = rules[refused]
    return scope, max(1, math.ceil(window - (now % window)))


def _hit_local(rules: list[tuple[str, str, int, int]]) -> tuple[str, int] | None:
    for scope, ident, cap, window in rules:
        wait = _hit(scope, ident, cap, window)
        if wait is not None:
            return scope, wait
    return None


def _client_ip(request: Request) -> str:
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # The last entry is the one appended by our own trusted proxy; earlier ones are client-supplied.
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def limit(kind: str):
    """Dependency factory: `kind` is "ai" (Gemini-backed screens) or "diagnosis" (photo upload, the costliest)."""

    def check(request: Request, user: User = Depends(current_user)) -> None:
        s = get_settings()
        if not s.rate_limit_enabled:
            return
        if kind == "diagnosis":
            per_minute, per_day = s.diagnosis_rate_per_minute, s.diagnosis_rate_per_day
        else:
            per_minute, per_day = s.ai_rate_per_minute, s.ai_rate_per_day
        rules = [
            (f"{kind}-min", user.uid, per_minute, 60),
            (f"{kind}-day", user.uid, per_day, 86_400),
            ("ip-min", _client_ip(request), s.ip_rate_per_minute, 60),
        ]
        try:
            refused = _hit_shared(rules) if s.redis_url else _hit_local(rules)
        except shared_store.StoreUnavailable:
            refused = _hit_local(rules)  # Redis is down: fall back to this instance's own counters
        if refused is not None:
            scope, wait = refused
            metrics.RATE_LIMITED.labels(scope).inc()
            raise HTTPException(429, "Too many requests. Please wait a little and try again.",
                                headers={"Retry-After": str(wait)})

    return check
