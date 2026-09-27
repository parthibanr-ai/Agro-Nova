"""Localization of dynamic content via Google Cloud Translation.

A middleware (see main.py) walks every JSON response and translates human-readable strings into the
caller's language (`?lang=` or Accept-Language). Identifiers, URLs, codes and dates are skipped.
Without TRANSLATE_API_KEY the content is returned untouched (English).
"""

import logging
import re
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.languages import get_language

logger = logging.getLogger(__name__)

_API = "https://translation.googleapis.com/language/translate/v2"
_CACHE: dict[tuple[str, str], str] = {}
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


def _collect(obj: Any, key: str | None, out: dict[str, None]) -> None:
    if isinstance(obj, str):
        if _translatable(key, obj):
            out[obj] = None
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _collect(v, k, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, out)


def _apply(obj: Any, key: str | None, table: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return table.get(obj, obj) if _translatable(key, obj) else obj
    if isinstance(obj, dict):
        return {k: _apply(v, k, table) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_apply(v, key, table) for v in obj]
    return obj


def translate_texts(texts: list[str], target_google_code: str) -> dict[str, str]:
    """Translate texts (cached). On any failure, returns the originals so the app keeps working."""
    key = get_settings().translate_api_key
    result: dict[str, str] = {}
    pending = []
    for t in texts:
        cached = _CACHE.get((target_google_code, t))
        if cached is not None:
            result[t] = cached
        else:
            pending.append(t)
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
                    for src, item in zip(chunk, resp.json()["data"]["translations"], strict=True):
                        _CACHE[(target_google_code, src)] = item["translatedText"]
                        result[src] = item["translatedText"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Translation failed, serving English: %s", exc)
    for t in pending:
        result.setdefault(t, t)
    return result


def localize_payload(payload: Any, lang_code: str | None) -> Any:
    lang = get_language(lang_code)
    if lang.code == "en":
        return payload
    target = lang.google_code if lang.machine_translation else get_language(lang.fallback).google_code
    if target == "en":
        return payload
    strings: dict[str, None] = {}
    _collect(payload, None, strings)
    if not strings:
        return payload
    return _apply(payload, None, translate_texts(list(strings), target))
