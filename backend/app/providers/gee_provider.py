"""Google Earth Engine provider: satellite-derived observed conditions over the exact plot polygon.

Datasets (all in the public EE catalog):
  - UCSB-CHG/CHIRPS/DAILY             rainfall (5 km), 1981-present  -> rain + historic normal
  - ECMWF/ERA5_LAND/DAILY_AGGR        2 m temperature
  - COPERNICUS/S2_SR_HARMONIZED       Sentinel-2 surface reflectance -> NDVI (10 m)
  - NASA/SMAP/SPL4SMGP/007            SMAP L4 soil moisture (surface)

The plot is small relative to CHIRPS/ERA5/SMAP pixels; those bands are effectively point values.
Sentinel-2 NDVI is the plot-resolution signal (needs cloud-free scenes; falls back to None).
"""

from datetime import date, timedelta

from app.providers.base import ClimateProvider, DailyForecast, ObservedClimate
from app.providers.open_meteo import OpenMeteoProvider

NORMAL_YEARS = 10


class GEEProvider(ClimateProvider):
    name = "gee"

    def __init__(self) -> None:
        self._forecast_fallback = OpenMeteoProvider()

    def observed(self, corners: list[tuple[float, float]], window_days: int) -> ObservedClimate:
        import ee

        geom = ee.Geometry.Polygon([[(lon, lat) for lat, lon in corners]])
        end = date.today()
        start = end - timedelta(days=window_days)

        def window_sum_rain(s: date, e: date) -> ee.Number:
            img = (
                ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
                .filterDate(s.isoformat(), e.isoformat())
                .select("precipitation")
                .sum()
            )
            return ee.Number(img.reduceRegion(ee.Reducer.mean(), geom, 5566).get("precipitation"))

        rain = window_sum_rain(start, end).getInfo()
        normals = [
            window_sum_rain(start.replace(year=start.year - k), end.replace(year=end.year - k))
            for k in range(1, NORMAL_YEARS + 1)
        ]
        normal = ee.List(normals).reduce(ee.Reducer.mean()).getInfo()

        tmean_k = (
            ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
            .filterDate(start.isoformat(), end.isoformat())
            .select("temperature_2m")
            .mean()
            .reduceRegion(ee.Reducer.mean(), geom, 11132)
            .get("temperature_2m")
        )
        tmean = ee.Number(tmean_k).subtract(273.15).getInfo()

        def ndvi_mean(s: date, e: date):
            col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(geom)
                .filterDate(s.isoformat(), e.isoformat())
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
                .map(lambda im: im.normalizedDifference(["B8", "B4"]).rename("ndvi"))
            )
            return col.mean().reduceRegion(ee.Reducer.mean(), geom, 10).get("ndvi")

        try:
            ndvi = ee.Number(ndvi_mean(end - timedelta(days=30), end)).getInfo()
        except Exception:  # noqa: BLE001 - cloud cover can leave no scenes
            ndvi = None
        try:
            ndvi_normal = ee.Number(ndvi_mean(
                (end - timedelta(days=30)).replace(year=end.year - 1), end.replace(year=end.year - 1)
            )).getInfo()
        except Exception:  # noqa: BLE001
            ndvi_normal = None

        try:
            sm = (
                ee.ImageCollection("NASA/SMAP/SPL4SMGP/007")
                .filterDate((end - timedelta(days=7)).isoformat(), end.isoformat())
                .select("sm_surface")
                .mean()
                .reduceRegion(ee.Reducer.mean(), geom, 11000)
                .get("sm_surface")
            )
            soil_moisture = ee.Number(sm).multiply(100).getInfo()
        except Exception:  # noqa: BLE001
            soil_moisture = None

        return ObservedClimate(
            window_days=window_days,
            rain_mm=round(rain, 1) if rain is not None else None,
            rain_normal_mm=round(normal, 1) if normal is not None else None,
            tmean_c=round(tmean, 1),
            ndvi=round(ndvi, 3) if ndvi is not None else None,
            ndvi_normal=round(ndvi_normal, 3) if ndvi_normal is not None else None,
            soil_moisture_pct=round(soil_moisture, 1) if soil_moisture is not None else None,
            sources=["CHIRPS", "ERA5-Land", "Sentinel-2", "NASA SMAP"],
        )

    def forecast(self, lat: float, lon: float, days: int) -> list[DailyForecast]:
        # Short-range forecast comes from a numerical weather model; NOAA GFS is also in the EE catalog
        # (NOAA/GFS0P25) and can replace this call when 6-hourly bands need to be sampled inside EE.
        return self._forecast_fallback.forecast(lat, lon, days)
