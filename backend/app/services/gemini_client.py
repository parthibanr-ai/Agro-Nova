"""Shared Gemini call used by diagnosis, crop recommendation and personalised advice.

Gemini intermittently answers 503 "high demand" (or 429) for a given model. Retry briefly, then fall back to
GEMINI_FALLBACK_MODEL (a comma-separated list, tried in order), so a short spike does not fail the farmer's request. Other errors (bad key, bad request)
are raised immediately.

Under a wider outage naive retrying multiplies the load on Gemini and pins a worker thread for tens of seconds
per farmer. So each request has a total time budget, and a per-model circuit breaker stops calling a model that
keeps failing until a cooldown has passed (later requests then fail fast instead of queueing up).
"""

import json
import logging
import random
import threading
import time

from app.core.config import get_settings

logger = logging.getLogger(__name__)

ATTEMPTS_PER_MODEL = 3
RETRY_DELAY_S = 1.5  # doubled after each failed attempt, with +/-50% jitter so retries do not synchronise
TOTAL_BUDGET_S = 20.0  # stop retrying once one request has spent this long
HTTP_TIMEOUT_MS = 30_000  # per call, so a hung connection cannot hold a worker forever
BREAKER_THRESHOLD = 3  # consecutive overload errors on a model before it is skipped
BREAKER_COOLDOWN_S = 30.0
RETRYABLE = (429, 500, 503, 504)


class GeminiUnavailable(RuntimeError):
    """Every configured model is paused after repeated overload errors (circuit open)."""


_lock = threading.Lock()
_client = None
_client_key: tuple | None = None
_failures: dict[str, int] = {}  # model -> consecutive retryable failures
_open_until: dict[str, float] = {}  # model -> monotonic time the breaker closes


def reset_state() -> None:
    """Forget the cached client and breaker state (used by tests and after config changes)."""
    global _client, _client_key
    with _lock:
        _client = None
        _client_key = None
        _failures.clear()
        _open_until.clear()


def _get_client(s):
    """One client per configuration, reused across requests (it holds the connection pool)."""
    global _client, _client_key
    from google import genai
    from google.genai import types

    key = (s.gemini_use_vertex, s.gemini_api_key, s.gee_cloud_project, s.gcp_location)
    with _lock:
        if _client is None or key != _client_key:
            http_options = types.HttpOptions(timeout=HTTP_TIMEOUT_MS)
            if s.gemini_use_vertex:
                _client = genai.Client(vertexai=True, project=s.gee_cloud_project, location=s.gcp_location,
                                       http_options=http_options)
            else:
                _client = genai.Client(api_key=s.gemini_api_key, http_options=http_options)
            _client_key = key
        return _client


def _is_open(model: str) -> bool:
    with _lock:
        return _open_until.get(model, 0.0) > time.monotonic()


def _record_success(model: str) -> None:
    with _lock:
        _failures.pop(model, None)
        _open_until.pop(model, None)


def _record_failure(model: str) -> None:
    with _lock:
        n = _failures.get(model, 0) + 1
        _failures[model] = n
        if n >= BREAKER_THRESHOLD:
            _open_until[model] = time.monotonic() + BREAKER_COOLDOWN_S
            logger.warning("Gemini %s paused for %.0fs after %d consecutive failures", model, BREAKER_COOLDOWN_S, n)


def generate_json(contents: list, temperature: float) -> dict:
    from google.genai import errors, types

    s = get_settings()
    client = _get_client(s)
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=temperature)

    models = [s.gemini_model]
    for m in (s.gemini_fallback_model or "").split(","):
        m = m.strip()
        if m and m not in models:
            models.append(m)

    start = time.monotonic()
    last: Exception | None = None
    for model in models:
        if _is_open(model):
            continue
        for attempt in range(ATTEMPTS_PER_MODEL):
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                result = json.loads(resp.text)
                _record_success(model)
                return result
            except errors.APIError as e:
                if e.code not in RETRYABLE:
                    raise
                last = e
                logger.warning("Gemini %s returned %s (attempt %d)", model, e.code, attempt + 1)
                _record_failure(model)
                if _is_open(model) or attempt == ATTEMPTS_PER_MODEL - 1:
                    break  # nothing to wait for: the next step is another model (or giving up)
                delay = RETRY_DELAY_S * 2 ** attempt * random.uniform(0.5, 1.5)
                if time.monotonic() - start + delay > TOTAL_BUDGET_S:
                    raise last
                time.sleep(delay)
        if time.monotonic() - start > TOTAL_BUDGET_S:
            break
    if last is None:
        raise GeminiUnavailable("Gemini is temporarily overloaded. Try again shortly.")
    raise last
