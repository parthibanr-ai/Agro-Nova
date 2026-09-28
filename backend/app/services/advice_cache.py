"""Share one Gemini answer between farmers whose plots are, for advice purposes, the same.

Each AI screen used to call Gemini afresh for every farmer. But the answer depends only on the text of the prompt
(state, crop, area, soil, recent weather, ENSO, livestock, today's date), and thousands of farmers in a state growing
the same crop under the same weather produce nearly the same prompt. So:

  1. `bucket_inputs()` rounds the numbers that go into the prompt (rain to 5-10 mm, temperature to 1 C, NDVI to 0.05,
     area to half a hectare, soil values to two significant figures). Advice is qualitative, so this changes no
     decision, and the rounded numbers are what the advice text quotes, so it stays consistent with what it saw.
  2. `cached_call()` keys the cache on a hash of the final prompt, so *anything* that affects the answer is part of
     the key by construction, and the date in the prompt refreshes the advice daily.

Failures are never cached (see `TTLCache`), and simultaneous identical requests share one Gemini call.
"""

import copy
import dataclasses
import hashlib
from collections.abc import Callable

from app.core.cache import TTLCache
from app.core.config import get_settings
from app.providers.base import ObservedClimate

_cache = TTLCache("ai-advice", max_entries=100_000, shared=True)


def _step(x: float, step: float) -> float:
    return round(round(x / step) * step, 4)


def _mm(x: float) -> float:
    """Rainfall to the nearest 5 mm (10 mm above 50), never rounding a non-zero normal down to zero."""
    r = _step(x, 10 if x >= 50 else 5)
    return r if r > 0 or x <= 0 else 5.0


def _sig(x: float, digits: int = 2) -> float:
    return float(f"{x:.{digits}g}")


def _opt(x, fn):
    return None if x is None else fn(x)


def bucket_observed(o: ObservedClimate | None) -> ObservedClimate | None:
    if o is None:
        return None
    return dataclasses.replace(
        o,
        rain_mm=_opt(o.rain_mm, _mm), rain_normal_mm=_opt(o.rain_normal_mm, _mm),
        tmean_c=_opt(o.tmean_c, lambda v: _step(v, 1.0)),
        ndvi=_opt(o.ndvi, lambda v: _step(v, 0.05)), ndvi_normal=_opt(o.ndvi_normal, lambda v: _step(v, 0.05)),
        soil_moisture_pct=_opt(o.soil_moisture_pct, lambda v: _step(v, 5.0)),
        sources=list(o.sources),
    )


def bucket_soil(values: dict) -> dict:
    out = {}
    for key, item in (values or {}).items():
        if isinstance(item, dict) and isinstance(item.get("value"), (int, float)):
            out[key] = {**item, "value": _sig(item["value"])}
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out[key] = _sig(item)  # farmer-entered sample
        else:
            out[key] = item
    return out


def bucket_area_ha(area_ha: float) -> float:
    return max(0.5, round(area_ha * 2) / 2)


def bucket_inputs(area_ha: float, soil_values: dict, observed: ObservedClimate | None):
    return bucket_area_ha(area_ha), bucket_soil(soil_values), bucket_observed(observed)


def cached_call(prompt: str, call: Callable[[str], dict]) -> dict:
    """Run `call(prompt)` at most once per distinct prompt per TTL; each caller gets its own copy of the result."""
    key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    result = _cache.get_or_compute(key, lambda: call(prompt), ttl=get_settings().ai_cache_ttl_s)
    return copy.deepcopy(result)
