import dataclasses
import logging
from datetime import date

from app.core.cache import TTLCache
from app.core.config import get_settings
from app.core.gee_auth import is_earth_engine_ready
from app.domain.grid import CELL_DEG, snap as _snap
from app.providers.base import ClimateProvider, ObservedClimate
from app.providers.gee_provider import GEEProvider
from app.providers.open_meteo import OpenMeteoProvider

logger = logging.getLogger(__name__)


# Satellite rainfall (CHIRPS) trails real time by weeks. "Rain in the last 30 days" that really describes last
# month would mislead a farmer after a dry spell, so a window older than this falls back to the fresher source.
MAX_RAIN_LAG_DAYS = 7

# Rain and weather products are 5-25 km wide, so every farm inside one 0.05 degree (~5.5 km) cell gets the same
# answer. Sharing one lookup per cell per day is what lets thousands of farmers share a few upstream calls.
_EE_FAILURE_TTL_S = 300  # a broken Earth Engine is not retried by every request, only every few minutes
_FAILED = object()

_satellite_cache = TTLCache("satellite-plot", max_entries=20_000)  # per plot: NDVI is plot-resolution
_weather_cache = TTLCache("weather-cell", max_entries=50_000)
_forecast_cache = TTLCache("forecast-cell", max_entries=50_000)


def _plot_key(corners) -> tuple:
    return tuple((round(lat, 5), round(lon, 5)) for lat, lon in corners)


def _copy(o: ObservedClimate) -> ObservedClimate:
    """Callers may adjust what they receive, so never hand out the cached object itself."""
    return dataclasses.replace(o, sources=list(o.sources))


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

    def _earth_engine(self, corners, window_days) -> ObservedClimate | None:
        """This plot's Earth Engine result (cached for the day), or None if Earth Engine is off or failing."""
        if not is_earth_engine_ready():
            return None
        key = ("ee", _plot_key(corners), window_days, date.today())

        def compute():
            try:
                return self._gee.observed(corners, window_days)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GEE observed() failed, falling back: %s", exc)
                _satellite_cache.set(key, _FAILED, _EE_FAILURE_TTL_S)
                return _FAILED

        result = _satellite_cache.get_or_compute(key, compute, ttl=get_settings().climate_cache_ttl_s,
                                                 cache_if=lambda v: v is not _FAILED)
        return None if result is _FAILED else result

    def _cell_weather(self, lat: float, lon: float, window_days: int) -> ObservedClimate:
        cell = (_snap(lat), _snap(lon))
        return _weather_cache.get_or_compute(
            ("om", *cell, window_days, date.today()),
            lambda: self._fallback.observed([cell], window_days),
            ttl=get_settings().climate_cache_ttl_s,
        )

    def observed(self, corners, window_days):
        gee = self._earth_engine(corners, window_days)
        if gee is not None and _rain_is_fresh(gee):
            return _copy(gee)
        lat = sum(p[0] for p in corners) / len(corners)
        lon = sum(p[1] for p in corners) / len(corners)
        base = _copy(self._cell_weather(lat, lon, window_days))
        if gee is not None:
            base.ndvi, base.ndvi_normal, base.soil_moisture_pct = gee.ndvi, gee.ndvi_normal, gee.soil_moisture_pct
            base.sources = base.sources + [
                name for name, value in (("Sentinel-2", gee.ndvi), ("NASA SMAP", gee.soil_moisture_pct)) if value is not None
            ]
        return base

    def forecast(self, lat, lon, days):
        cell = (_snap(lat), _snap(lon))
        return list(_forecast_cache.get_or_compute(
            ("fc", *cell, days, date.today()),
            lambda: self._fallback.forecast(*cell, days),
            ttl=get_settings().forecast_cache_ttl_s,
        ))


_provider: ClimateProvider | None = None


def get_climate_provider() -> ClimateProvider:
    global _provider
    if _provider is None:
        _provider = ResilientProvider()
    return _provider
