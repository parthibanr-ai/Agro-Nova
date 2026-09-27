"""Open-Meteo provider: keyless fallback for observed history and a 16-day forecast."""

from datetime import date, timedelta
from statistics import mean

import httpx

from app.providers.base import ClimateProvider, DailyForecast, ObservedClimate

_FORECAST = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


class OpenMeteoProvider(ClimateProvider):
    name = "open-meteo"

    def _get(self, url: str, params: dict) -> dict:
        resp = httpx.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def observed(self, corners: list[tuple[float, float]], window_days: int) -> ObservedClimate:
        lat = sum(p[0] for p in corners) / len(corners)
        lon = sum(p[1] for p in corners) / len(corners)
        end = date.today() - timedelta(days=2)  # archive lags ~2 days
        start = end - timedelta(days=window_days - 1)

        def window(year_shift: int) -> tuple[float, float]:
            s, e = (d.replace(year=d.year - year_shift) for d in (start, end))
            data = self._get(
                _ARCHIVE,
                {"latitude": lat, "longitude": lon, "start_date": s.isoformat(), "end_date": e.isoformat(),
                 "daily": "precipitation_sum,temperature_2m_mean", "timezone": "auto"},
            )["daily"]
            rain = sum(v for v in data["precipitation_sum"] if v is not None)
            temps = [v for v in data["temperature_2m_mean"] if v is not None]
            return rain, (mean(temps) if temps else float("nan"))

        rain, tmean = window(0)
        normals = []
        for shift in range(1, 6):
            try:
                normals.append(window(shift)[0])
            except Exception:  # noqa: BLE001 - normals are best-effort
                continue
        return ObservedClimate(
            window_days=window_days,
            rain_mm=round(rain, 1),
            rain_normal_mm=round(mean(normals), 1) if normals else None,
            tmean_c=round(tmean, 1),
            sources=["Open-Meteo archive (ERA5)"],
        )

    def forecast(self, lat: float, lon: float, days: int) -> list[DailyForecast]:
        data = self._get(
            _FORECAST,
            {"latitude": lat, "longitude": lon, "forecast_days": min(days, 16), "timezone": "auto",
             "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,et0_fao_evapotranspiration"},
        )["daily"]
        return [
            DailyForecast(
                day=date.fromisoformat(d),
                tmax_c=tmax,
                tmin_c=tmin,
                rain_mm=rain or 0.0,
                et0_mm=et0,
            )
            for d, tmax, tmin, rain, et0 in zip(
                data["time"],
                data["temperature_2m_max"],
                data["temperature_2m_min"],
                data["precipitation_sum"],
                data["et0_fao_evapotranspiration"],
                strict=True,
            )
        ]
