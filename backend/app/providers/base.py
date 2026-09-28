from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ObservedClimate:
    """Recent conditions over the plot, compared with the historic normal for the same window."""

    window_days: int
    rain_mm: float | None = None
    rain_normal_mm: float | None = None
    tmean_c: float | None = None
    ndvi: float | None = None
    ndvi_normal: float | None = None
    soil_moisture_pct: float | None = None  # volumetric, root zone / surface
    as_of: date | None = None  # last day covered by the rain/temperature window (satellite rain products lag)
    sources: list[str] = field(default_factory=list)


@dataclass
class DailyForecast:
    day: date
    tmax_c: float
    tmin_c: float
    rain_mm: float
    et0_mm: float | None = None


class ClimateProvider(ABC):
    """Swap-in point for other satellite/climate backends (Bhuvan, national met services, commercial)."""

    name: str

    @abstractmethod
    def observed(self, corners: list[tuple[float, float]], window_days: int) -> ObservedClimate: ...

    @abstractmethod
    def forecast(self, lat: float, lon: float, days: int) -> list[DailyForecast]: ...
