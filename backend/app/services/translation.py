"""Localization of dynamic content via Google Cloud Translation.

A middleware (see main.py) walks every JSON response and translates human-readable strings into the
caller's language (`?lang=` or Accept-Language). Identifiers, URLs, codes and dates are skipped.
Without TRANSLATE_API_KEY the content is returned untouched (English).

Text that Gemini already wrote in the caller's language is not sent for translation (that would be paid for and
wrong, the source is not English). A dict says which of its keys are like that with
`"_localized": {"lang": "hi", "keys": ["summary", "items"]}`; the marker is honoured only when it names the language
being served, and is always removed from the response.
"""

import logging
import re
from typing import Any

import httpx

from app.core.cache import TTLCache
from app.core.config import get_settings
from app.core.languages import get_language

logger = logging.getLogger(__name__)

_API = "https://translation.googleapis.com/language/translate/v2"
# Bounded LRU: dynamic advice is largely unique text, so an unbounded dict would grow until the process dies.
_CACHE = TTLCache("translation", max_entries=get_settings().translation_cache_max_entries,
                  default_ttl=7 * 24 * 3600, shared=True)
_BATCH = 100

# Keys whose string values are machine-readable, never translated.
SKIP_KEYS = {
    "id", "uid", "code", "url", "lang", "language", "country", "state", "crop", "crop_id", "kind",
    "status", "severity", "phase", "source_id", "condition_id", "icon", "type", "date", "unit",
    "currency", "google_code", "group", "name_key", "fcm_token", "sowing_date", "created_at",
}
_MACHINE = re.compile(r"^[a-z0-9_\-./:@#+?=&%]*$")
_DATELIKE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _translatable(key: str | None, value: str) -> bool:
    if not value.strip() or (key and (key in SKIP_KEYS or key.endswith(("_id", "_url", "_code")))):
        return False
    if value.startswith(("http://", "https://")) or _DATELIKE.match(value):
        return False
    return not _MACHINE.match(value)


LOCALIZED_MARK = "_localized"


def localized_marker(lang_code: str, keys: list[str]) -> dict:
    """Mark keys of a response dict as already written in `lang_code` (see the module docstring)."""
    return {"lang": lang_code, "keys": keys}


def _already_in(obj: dict, lang_code: str) -> set[str]:
    mark = obj.get(LOCALIZED_MARK)
    if isinstance(mark, dict) and str(mark.get("lang", "")).lower() == lang_code.lower():
        return set(mark.get("keys") or ())
    return set()


def _collect(obj: Any, key: str | None, out: dict[str, None], lang_code: str = "") -> None:
    if isinstance(obj, str):
        if _translatable(key, obj):
            out[obj] = None
    elif isinstance(obj, dict):
        skip = _already_in(obj, lang_code)
        for k, v in obj.items():
            if k != LOCALIZED_MARK and k not in skip:
                _collect(v, k, out, lang_code)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, out, lang_code)


def _apply(obj: Any, key: str | None, table: dict[str, str], lang_code: str = "") -> Any:
    if isinstance(obj, str):
        return table.get(obj, obj) if _translatable(key, obj) else obj
    if isinstance(obj, dict):
        skip = _already_in(obj, lang_code)
        return {k: (v if k in skip else _apply(v, k, table, lang_code))
                for k, v in obj.items() if k != LOCALIZED_MARK}
    if isinstance(obj, list):
        return [_apply(v, key, table, lang_code) for v in obj]
    return obj


def _strip_marks(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_marks(v) for k, v in obj.items() if k != LOCALIZED_MARK}
    if isinstance(obj, list):
        return [_strip_marks(v) for v in obj]
    return obj


def translate_texts(texts: list[str], target_google_code: str) -> dict[str, str]:
    """Translate texts (cached). On any failure, returns the originals so the app keeps working."""
    key = get_settings().translate_api_key
    result: dict[str, str] = {}
    pending = []
    for t in texts:
        hit, cached = _CACHE.get((target_google_code, t))
        if hit:
            result[t] = cached
        else:
            pending.append(t)
    if pending:
        # Another instance may have translated these already (one Redis round trip for all of them).
        for (_, src), translated in _CACHE.shared_get_many([(target_google_code, t) for t in pending]).items():
            _CACHE.set((target_google_code, src), translated)
            result[src] = translated
        pending = [t for t in pending if t not in result]
    if pending and key:
        try:
            with httpx.Client(timeout=15) as client:
                for i in range(0, len(pending), _BATCH):
                    chunk = pending[i : i + _BATCH]
                    resp = client.post(
                        _API,
                        params={"key": key},
                        json={"q": chunk, "target": target_google_code, "source": "en", "format": "text"},
                    )
                    resp.raise_for_status()
                    fresh = {}
                    for src, item in zip(chunk, resp.json()["data"]["translations"], strict=True):
                        _CACHE.set((target_google_code, src), item["translatedText"])
                        result[src] = fresh[(target_google_code, src)] = item["translatedText"]
                    _CACHE.shared_set_many(fresh, _CACHE.default_ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Translation failed, serving English: %s", exc)
    for t in pending:
        result.setdefault(t, t)
    return result


def localize_payload(payload: Any, lang_code: str | None) -> Any:
    lang = get_language(lang_code)
    if lang.code == "en":
        return _strip_marks(payload)
    target = lang.google_code if lang.machine_translation else get_language(lang.fallback).google_code
    if target == "en":
        return _strip_marks(payload)
    strings: dict[str, None] = {}
    _collect(payload, None, strings, lang.code)
    if not strings:
        return _strip_marks(payload)
    return _apply(payload, None, translate_texts(list(strings), target), lang.code)
