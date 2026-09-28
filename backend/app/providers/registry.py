import logging
from datetime import date

from app.core.gee_auth import is_earth_engine_ready
from app.providers.base import ClimateProvider, ObservedClimate
from app.providers.gee_provider import GEEProvider
from app.providers.open_meteo import OpenMeteoProvider

logger = logging.getLogger(__name__)


# Satellite rainfall (CHIRPS) trails real time by weeks. "Rain in the last 30 days" that really describes last
# month would mislead a farmer after a dry spell, so a window older than this falls back to the fresher source.
MAX_RAIN_LAG_DAYS = 7


def _rain_is_fresh(o: ObservedClimate) -> bool:
    return (o.rain_mm is not None and o.rain_normal_mm is not None and o.as_of is not None
            and (date.today() - o.as_of).days <= MAX_RAIN_LAG_DAYS)


class ResilientProvider(ClimateProvider):
    """Earth Engine when it is up and current; Open-Meteo for fresh rain/temperature otherwise.

    Open-Meteo has no vegetation or soil-moisture signal, so when Earth Engine's rainfall is too stale to use we
    still keep its NDVI and soil moisture and merge them into the Open-Meteo result.
    """

    name = "gee+open-meteo"

    def __init__(self) -> None:
        self._gee = GEEProvider()
        self._fallback = OpenMeteoProvider()

    def observed(self, corners, window_days):
        gee = None
        if is_earth_engine_ready():
            try:
                gee = self._gee.observed(corners, window_days)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GEE observed() failed, falling back: %s", exc)
        if gee is not None and _rain_is_fresh(gee):
            return gee
        base = self._fallback.observed(corners, window_days)
        if gee is not None:
            base.ndvi, base.ndvi_normal, base.soil_moisture_pct = gee.ndvi, gee.ndvi_normal, gee.soil_moisture_pct
            base.sources = base.sources + [
                name for name, value in (("Sentinel-2", gee.ndvi), ("NASA SMAP", gee.soil_moisture_pct)) if value is not None
            ]
        return base

    def forecast(self, lat, lon, days):
        return self._fallback.forecast(lat, lon, days)


_provider: ClimateProvider | None = None


def get_climate_provider() -> ClimateProvider:
    global _provider
    if _provider is None:
        _provider = ResilientProvider()
    return _provider
