"""Forward geocoding for the plot-capture map's location search.

Uses OpenStreetMap Nominatim (free, public, no API key) rather than the Google Places API, so this
works even before a real Google Maps key is configured. Nominatim's usage policy requires a real
User-Agent identifying the app; see https://operations.osmfoundation.org/policies/nominatim/.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "AgroNova/1.0 (https://github.com/parthibanr-ai/Agro-Nova)"


def search(query: str, limit: int = 5) -> list[dict]:
    query = query.strip()
    if not query:
        return []
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
        logger.warning("Geocoding unavailable: %s", exc)
        return []
