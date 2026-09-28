"""Forward geocoding for the plot-capture map's location search.

Tries OpenStreetMap Nominatim first (free, public, no API key), so search works even before a Google
Maps key is configured. Nominatim's usage policy requires a real User-Agent identifying the app; see
https://operations.osmfoundation.org/policies/nominatim/.

Nominatim's coverage of small Indian villages/hamlets is patchy, so if it finds nothing and
`GOOGLE_MAPS_API_KEY` is set (with the Geocoding API enabled), we fall back to Google's Geocoding API,
which has much better coverage there.
"""

import logging

import httpx

from app.core.cache import TTLCache
from app.core.config import get_settings

logger = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
USER_AGENT = "AgroNova/1.0 (https://github.com/parthibanr-ai/Agro-Nova)"


_cache = TTLCache("geocode", max_entries=50_000, default_ttl=24 * 3600)


def search(query: str, limit: int = 5) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    # Village and district names repeat constantly; this also keeps us inside Nominatim's usage policy.
    return list(_cache.get_or_compute(("q", query.lower(), limit), lambda: _search(query, limit) or None,
                                      negative_ttl=600) or [])


def _search(query: str, limit: int) -> list[dict]:
    results = _search_nominatim(query, limit)
    if not results:
        results = _search_google(query, limit)
    return results


def _search_nominatim(query: str, limit: int) -> list[dict]:
    try:
        resp = httpx.get(
            NOMINATIM,
            params={"q": query, "format": "jsonv2", "limit": limit},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"display_name": item["display_name"], "lat": float(item["lat"]), "lon": float(item["lon"])}
            for item in resp.json()
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nominatim geocoding unavailable: %s", exc)
        return []


def _search_google(query: str, limit: int) -> list[dict]:
    key = get_settings().google_maps_api_key
    if not key:
        return []
    try:
        resp = httpx.get(GOOGLE_GEOCODE, params={"address": query, "key": key}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            logger.warning("Google geocoding error: %s", data.get("status"))
            return []
        return [
            {
                "display_name": item["formatted_address"],
                "lat": item["geometry"]["location"]["lat"],
                "lon": item["geometry"]["location"]["lng"],
            }
            for item in data.get("results", [])[:limit]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google geocoding unavailable: %s", exc)
        return []
