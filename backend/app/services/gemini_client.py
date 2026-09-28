"""Shared model call used by diagnosis, crop recommendation and personalised advice.

Vendors are tried in LLM_ORDER (default OpenAI, then Anthropic, then Gemini); the first to answer wins and a vendor
without a key is skipped. The rest of this note is about the Gemini leg.

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

from app.core import metrics
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
_slots: threading.BoundedSemaphore | None = None
_slots_size = 0


def reset_state() -> None:
    """Forget the cached client, breaker state and concurrency slots (tests, and after config changes)."""
    global _client, _client_key, _slots, _slots_size
    with _lock:
        _client = None
        _client_key = None
        _slots = None
        _slots_size = 0
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


def circuit_states() -> dict[str, bool]:
    """Model -> whether its circuit breaker is currently open (for /metrics)."""
    now = time.monotonic()
    with _lock:
        return {m: until > now for m, until in _open_until.items()}


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


def _get_slots(size: int) -> threading.BoundedSemaphore:
    global _slots, _slots_size
    with _lock:
        if _slots is None or _slots_size != size:
            _slots, _slots_size = threading.BoundedSemaphore(max(1, size)), size
        return _slots


def generate_json(contents: list, temperature: float) -> dict:
    """Call Gemini, holding one of GEMINI_MAX_CONCURRENCY slots for the whole attempt sequence.

    The slot cap keeps one instance inside its share of the project quota however many requests arrive; a request
    that cannot get a slot within GEMINI_QUEUE_TIMEOUT_S is told Gemini is busy instead of piling up threads.
    """
    s = get_settings()
    slots = _get_slots(s.gemini_max_concurrency)
    if not slots.acquire(timeout=s.gemini_queue_timeout_s):
        metrics.GEMINI_BUSY.inc()
        raise GeminiUnavailable("Gemini is busy right now. Please try again shortly.")
    try:
        return _generate(contents, temperature, s)
    finally:
        slots.release()


def _neutral(contents: list) -> list[tuple]:
    """Gemini-style contents (strings and image Parts) as ("text", str) / ("image", bytes, mime) tuples."""
    out: list[tuple] = []
    for c in contents:
        if isinstance(c, str):
            out.append(("text", c))
            continue
        blob = getattr(c, "inline_data", None)
        if blob is not None and getattr(blob, "data", None):
            out.append(("image", blob.data, blob.mime_type or "image/jpeg"))
        elif getattr(c, "text", None):
            out.append(("text", c.text))
    return out


def _post_json(url: str, headers: dict, body: dict, s) -> dict:
    import httpx

    r = httpx.post(url, headers=headers, json=body, timeout=s.llm_vendor_timeout_s)
    r.raise_for_status()
    return r.json()


def _openai_generate(parts: list[tuple], temperature: float, s) -> dict:
    """OpenAI chat completions over plain HTTP (no extra dependency), asking for a JSON object."""
    import base64

    content: list[dict] = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "text", "text": p[1]})
        else:
            uri = f"data:{p[2]};base64,{base64.b64encode(p[1]).decode()}"
            content.append({"type": "image_url", "image_url": {"url": uri}})
    data = _post_json(
        f"{s.openai_base_url.rstrip('/')}/chat/completions",
        {"Authorization": f"Bearer {s.openai_api_key}"},
        {"model": s.openai_model, "temperature": temperature, "response_format": {"type": "json_object"},
         "messages": [{"role": "user", "content": content}]}, s)
    return json.loads(data["choices"][0]["message"]["content"])


def _json_from_text(text: str) -> dict:
    """The JSON object in a model reply that may wrap it in prose or a code fence."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("the reply contained no JSON object")
    return json.loads(text[a:b + 1])


def _anthropic_generate(parts: list[tuple], temperature: float, s) -> dict:
    """Anthropic Messages API over plain HTTP. It has no JSON mode, so the object is cut out of the reply."""
    import base64

    content: list[dict] = []
    for p in parts:
        if p[0] == "text":
            content.append({"type": "text", "text": p[1]})
        else:
            content.append({"type": "image", "source": {"type": "base64", "media_type": p[2],
                                                        "data": base64.b64encode(p[1]).decode()}})
    data = _post_json(
        f"{s.anthropic_base_url.rstrip('/')}/messages",
        {"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"},
        {"model": s.anthropic_model, "max_tokens": 4096, "temperature": min(temperature, 1.0),
         "messages": [{"role": "user", "content": content}]}, s)
    return _json_from_text("".join(b.get("text", "") for b in data.get("content", [])))


# name -> (is configured?, call). The order in which they are tried is LLM_ORDER; a new vendor is one entry here.
VENDORS = {
    "openai": (lambda s: bool(s.openai_api_key), _openai_generate),
    "anthropic": (lambda s: bool(s.anthropic_api_key), _anthropic_generate),
}


def _configured_vendors(s) -> list[str]:
    """LLM_ORDER without the vendors that have no key. Gemini is configured by its key or by Vertex."""
    out = []
    for n in (x.strip().lower() for x in (s.llm_order or "").split(",")):
        if n == "gemini" and (s.gemini_api_key or s.gemini_use_vertex):
            out.append(n)
        elif n in VENDORS and VENDORS[n][0](s):
            out.append(n)
    return out


def any_vendor_configured(s) -> bool:
    return bool(_configured_vendors(s))


def _vendor_order(s) -> list[str]:
    return _configured_vendors(s) or ["gemini"]  # nothing configured: the Gemini path reports the missing key


def _generate(contents: list, temperature: float, s) -> dict:
    """Try each configured vendor in LLM_ORDER; the first to answer wins.

    A vendor that fails (overloaded, out of quota, timeout, bad key) is passed over, and OpenAI / Anthropic get a
    circuit breaker like the Gemini models so a vendor that is down is skipped at once instead of costing a timeout
    on every request. If every vendor fails, Gemini's error is raised when Gemini was tried (it is the one callers
    already understand), else the first vendor's error.
    """
    first: Exception | None = None
    gemini_error: Exception | None = None
    parts = None
    for name in _vendor_order(s):
        if name == "gemini":
            try:
                return _generate_gemini(contents, temperature, s)
            except Exception as e:  # noqa: BLE001
                gemini_error = e
                logger.warning("Gemini failed (%s)", type(e).__name__)
                continue
        if _is_open(name):
            metrics.GEMINI_CALLS.labels(name, "skipped_circuit_open").inc()
            continue
        parts = parts if parts is not None else _neutral(contents)
        try:
            result = VENDORS[name][1](parts, temperature, s)
        except Exception as e:  # noqa: BLE001 - any failure of one vendor means trying the next
            _record_failure(name)
            metrics.GEMINI_CALLS.labels(name, "error").inc()
            logger.warning("%s failed (%s)", name, type(e).__name__)
            first = first or e
            continue
        _record_success(name)
        metrics.GEMINI_CALLS.labels(name, "success").inc()
        return result
    raise gemini_error or first or GeminiUnavailable("No model vendor is available right now.")


def _generate_gemini(contents: list, temperature: float, s) -> dict:
    from google.genai import errors, types

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
            metrics.GEMINI_CALLS.labels(model, "skipped_circuit_open").inc()
            continue
        for attempt in range(ATTEMPTS_PER_MODEL):
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                result = json.loads(resp.text)
                _record_success(model)
                metrics.GEMINI_CALLS.labels(model, "success").inc()
                return result
            except errors.APIError as e:
                if e.code not in RETRYABLE:
                    metrics.GEMINI_CALLS.labels(model, "error").inc()
                    raise
                metrics.GEMINI_CALLS.labels(model, f"http_{e.code}").inc()
                last = e
                logger.warning("Gemini %s returned %s (attempt %d)", model, e.code, attempt + 1)
                _record_failure(model)
                if e.code == 429 or _is_open(model) or attempt == ATTEMPTS_PER_MODEL - 1:
                    # (a 429 is an exhausted quota, which waiting a few seconds does not fix)
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
