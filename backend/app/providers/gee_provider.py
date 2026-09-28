"""Google Earth Engine provider: satellite-derived observed conditions over the exact plot polygon.

Datasets (all in the public EE catalog):
  - UCSB-CHG/CHIRPS/DAILY             rainfall (5 km), 1981-present  -> rain + historic normal
  - ECMWF/ERA5_LAND/DAILY_AGGR        2 m temperature
  - COPERNICUS/S2_SR_HARMONIZED       Sentinel-2 surface reflectance -> NDVI (10 m)
  - NASA/SMAP/SPL4SMGP/008            SMAP L4 soil moisture (surface); 007 was superseded and stopped in 2025

The plot is small relative to CHIRPS/ERA5/SMAP pixels (5-11 km), so those bands are sampled at the plot's centre
point: reducing over a polygon smaller than one pixel returns nothing, because no pixel centre falls inside it.
CHIRPS also runs several weeks behind real time, so the rainfall window ends at its latest available day rather
than today (otherwise "last 30 days" would hold a few days of rain compared against a full-month normal).
Sentinel-2 NDVI is the plot-resolution signal (needs cloud-free scenes; falls back to None).
"""

from datetime import date, timedelta

from app.providers.base import ClimateProvider, DailyForecast, ObservedClimate
from app.providers.open_meteo import OpenMeteoProvider

NORMAL_YEARS = 10


def _years_ago(d: date, k: int) -> date:
    try:
        return d.replace(year=d.year - k)
    except ValueError:  # 29 February in a non-leap year
        return d.replace(year=d.year - k, day=28)


class GEEProvider(ClimateProvider):
    name = "gee"

    def __init__(self) -> None:
        self._forecast_fallback = OpenMeteoProvider()

    def observed(self, corners: list[tuple[float, float]], window_days: int) -> ObservedClimate:
        import ee

        geom = ee.Geometry.Polygon([[(lon, lat) for lat, lon in corners]])
        point = geom.centroid(1)  # rain / temperature / soil-moisture pixels are 5-11 km: sample the centre
        today = date.today()

        # CHIRPS lags real time by weeks; end the window at its latest day so it is a real `window_days` long.
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        latest = chirps.sort("system:time_start", False).first().date().format("YYYY-MM-dd").getInfo()
        end = min(today, date.fromisoformat(latest) + timedelta(days=1))  # filterDate's end is exclusive
        start = end - timedelta(days=window_days)

        def window_sum_rain(s: date, e: date) -> ee.Number:
            img = chirps.filterDate(s.isoformat(), e.isoformat()).select("precipitation").sum()
            return ee.Number(img.reduceRegion(ee.Reducer.mean(), point, 5566).get("precipitation"))

        rain = window_sum_rain(start, end).getInfo()
        normals = [
            window_sum_rain(_years_ago(start, k), _years_ago(end, k))
            for k in range(1, NORMAL_YEARS + 1)
        ]
        normal = ee.List(normals).reduce(ee.Reducer.mean()).getInfo()

        try:
            tmean_k = (
                ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
                .filterDate(start.isoformat(), end.isoformat())
                .select("temperature_2m")
                .mean()
                .reduceRegion(ee.Reducer.mean(), point, 11132)
                .get("temperature_2m")
                .getInfo()
            )
            tmean = tmean_k - 273.15 if tmean_k is not None else None
        except Exception:  # noqa: BLE001
            tmean = None

        def ndvi_mean(s: date, e: date):
            col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(geom)
                .filterDate(s.isoformat(), e.isoformat())
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
                .map(lambda im: im.normalizedDifference(["B8", "B4"]).rename("ndvi"))
            )
            return col.mean().reduceRegion(ee.Reducer.mean(), geom, 10).get("ndvi")

        # Sentinel-2 is near-real-time, so its window ends today, not at the rainfall cut-off.
        try:
            ndvi = ee.Number(ndvi_mean(today - timedelta(days=30), today)).getInfo()
        except Exception:  # noqa: BLE001 - cloud cover can leave no scenes
            ndvi = None
        try:
            ndvi_normal = ee.Number(ndvi_mean(
                _years_ago(today - timedelta(days=30), 1), _years_ago(today, 1)
            )).getInfo()
        except Exception:  # noqa: BLE001
            ndvi_normal = None

        try:
            sm = (
                ee.ImageCollection("NASA/SMAP/SPL4SMGP/008")
                .filterDate((today - timedelta(days=7)).isoformat(), today.isoformat())
                .select("sm_surface")
                .mean()
                .reduceRegion(ee.Reducer.mean(), point, 11000)
                .get("sm_surface")
            )
            soil_moisture = ee.Number(sm).multiply(100).getInfo()
        except Exception:  # noqa: BLE001
            soil_moisture = None

        sources = [name for name, value in (("CHIRPS", rain), ("ERA5-Land", tmean), ("Sentinel-2", ndvi),
                                            ("NASA SMAP", soil_moisture)) if value is not None]
        return ObservedClimate(
            window_days=window_days,
            rain_mm=round(rain, 1) if rain is not None else None,
            rain_normal_mm=round(normal, 1) if normal is not None else None,
            tmean_c=round(tmean, 1) if tmean is not None else None,
            ndvi=round(ndvi, 3) if ndvi is not None else None,
            ndvi_normal=round(ndvi_normal, 3) if ndvi_normal is not None else None,
            soil_moisture_pct=round(soil_moisture, 1) if soil_moisture is not None else None,
            as_of=end - timedelta(days=1),
            sources=sources,
        )

    def observed_many(self, plots: list[tuple[str, list[tuple[float, float]]]],
                      window_days: int) -> dict[str, ObservedClimate]:
        """The same numbers as `observed()` for many plots in a handful of Earth Engine requests.

        `plots` is [(key, corners)]. Building one image of all the bands and reducing it over a whole collection of
        plots costs about as much as one plot does alone, which is what makes an overnight run over every farm
        possible. Keep the plots of one call close together (sorted by area): the Sentinel-2 scene search covers all
        of them. Raises if the rain/temperature/soil request fails; a failed NDVI request only leaves NDVI empty.
        """
        import ee

        today = date.today()
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        latest = chirps.sort("system:time_start", False).first().date().format("YYYY-MM-dd").getInfo()
        end = min(today, date.fromisoformat(latest) + timedelta(days=1))
        start = end - timedelta(days=window_days)

        def rain_sum(s: date, e: date):
            return chirps.filterDate(s.isoformat(), e.isoformat()).select("precipitation").sum()

        normal = ee.ImageCollection(
            [rain_sum(_years_ago(start, k), _years_ago(end, k)) for k in range(1, NORMAL_YEARS + 1)]).mean()
        temperature = (ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(start.isoformat(), end.isoformat())
                       .select("temperature_2m").mean().subtract(273.15))
        moisture = (ee.ImageCollection("NASA/SMAP/SPL4SMGP/008")
                    .filterDate((today - timedelta(days=7)).isoformat(), today.isoformat())
                    .select("sm_surface").mean().multiply(100))
        coarse = (rain_sum(start, end).rename("rain").addBands(normal.rename("rain_normal"))
                  .addBands(temperature.rename("tmean")).addBands(moisture.rename("soil_moisture")))

        keys = [k for k, _ in plots]
        centres = []
        for key, corners in plots:
            lat = sum(p[0] for p in corners) / len(corners)
            lon = sum(p[1] for p in corners) / len(corners)
            centres.append(ee.Feature(ee.Geometry.Point([lon, lat]), {"k": key}))
        # Rain, temperature and soil moisture pixels are 5-11 km wide: sample the centre, as observed() does.
        sampled = coarse.reduceRegions(ee.FeatureCollection(centres), ee.Reducer.first(), 5566).getInfo()
        values = {f["properties"]["k"]: f["properties"] for f in sampled["features"]}

        ndvi_values: dict[str, dict] = {}
        try:
            polygons = [ee.Feature(ee.Geometry.Polygon([[(lon, lat) for lat, lon in corners]]), {"k": key})
                        for key, corners in plots]
            footprint = ee.Geometry.MultiPolygon([[[(lon, lat) for lat, lon in corners]] for _, corners in plots])

            def ndvi(s: date, e: date):
                return (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(footprint)
                        .filterDate(s.isoformat(), e.isoformat()).filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
                        .map(lambda im: im.normalizedDifference(["B8", "B4"]).rename("ndvi")).mean())

            both = (ndvi(today - timedelta(days=30), today).rename("ndvi")
                    .addBands(ndvi(_years_ago(today - timedelta(days=30), 1), _years_ago(today, 1)).rename("ndvi_normal")))
            reduced = both.reduceRegions(ee.FeatureCollection(polygons), ee.Reducer.mean(), 10).getInfo()
            ndvi_values = {f["properties"]["k"]: f["properties"] for f in reduced["features"]}
        except Exception:  # noqa: BLE001 - cloud cover can leave no scenes; keep the rest
            ndvi_values = {}

        def rounded(v, digits):
            return round(v, digits) if isinstance(v, (int, float)) else None

        out = {}
        for key in keys:
            c, n = values.get(key, {}), ndvi_values.get(key, {})
            rain, rain_normal, tmean = rounded(c.get("rain"), 1), rounded(c.get("rain_normal"), 1), rounded(c.get("tmean"), 1)
            ndvi_now, ndvi_prev = rounded(n.get("ndvi"), 3), rounded(n.get("ndvi_normal"), 3)
            moist = rounded(c.get("soil_moisture"), 1)
            out[key] = ObservedClimate(
                window_days=window_days, rain_mm=rain, rain_normal_mm=rain_normal, tmean_c=tmean, ndvi=ndvi_now,
                ndvi_normal=ndvi_prev, soil_moisture_pct=moist, as_of=end - timedelta(days=1),
                sources=[name for name, v in (("CHIRPS", rain), ("ERA5-Land", tmean), ("Sentinel-2", ndvi_now),
                                              ("NASA SMAP", moist)) if v is not None])
        return out

    def forecast(self, lat: float, lon: float, days: int) -> list[DailyForecast]:
        # Short-range forecast comes from a numerical weather model; NOAA GFS is also in the EE catalog
        # (NOAA/GFS0P25) and can replace this call when 6-hourly bands need to be sampled inside EE.
        return self._forecast_fallback.forecast(lat, lon, days)
