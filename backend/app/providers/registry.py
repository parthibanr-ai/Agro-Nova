import logging

from app.core.gee_auth import is_earth_engine_ready
from app.providers.base import ClimateProvider
from app.providers.gee_provider import GEEProvider
from app.providers.open_meteo import OpenMeteoProvider

logger = logging.getLogger(__name__)


class ResilientProvider(ClimateProvider):
    """Uses GEE when initialised, and degrades to Open-Meteo on any GEE failure."""

    name = "gee+open-meteo"

    def __init__(self) -> None:
        self._gee = GEEProvider()
        self._fallback = OpenMeteoProvider()

    def observed(self, corners, window_days):
        if is_earth_engine_ready():
            try:
                return self._gee.observed(corners, window_days)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GEE observed() failed, falling back: %s", exc)
        return self._fallback.observed(corners, window_days)

    def forecast(self, lat, lon, days):
        return self._fallback.forecast(lat, lon, days)


_provider: ClimateProvider | None = None


def get_climate_provider() -> ClimateProvider:
    global _provider
    if _provider is None:
        _provider = ResilientProvider()
    return _provider
