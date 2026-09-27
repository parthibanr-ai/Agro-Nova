"""Plot boundary validation (server is the source of truth; clients validate for UX only)."""

from pyproj import Geod
from shapely.geometry import Polygon as ShapelyPolygon

_GEOD = Geod(ellps="WGS84")

MIN_CORNERS = 4
MAX_CORNERS = 12
MIN_AREA_M2 = 50.0
MAX_AREA_M2 = 10_000_000.0  # 10 km^2 (1,000 ha)

Point = tuple[float, float]  # (lat, lon)


class PolygonValidationError(ValueError):
    pass


def validate_plot(points: list[Point], bbox: tuple[float, float, float, float] | None = None) -> None:
    """Raise PolygonValidationError unless `points` is a plausible farm boundary.

    bbox is (min_lat, min_lon, max_lat, max_lon) of the farmer's country, if known.
    """
    if not (MIN_CORNERS <= len(points) <= MAX_CORNERS):
        raise PolygonValidationError(
            f"A plot needs {MIN_CORNERS}-{MAX_CORNERS} corner points, got {len(points)}."
        )
    for lat, lon in points:
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise PolygonValidationError(f"Coordinate ({lat}, {lon}) is out of range.")
        if bbox and not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
            raise PolygonValidationError(
                f"Coordinate ({lat}, {lon}) is outside the selected country. "
                "Check the latitude/longitude order or the country."
            )
    shape = ShapelyPolygon([(lon, lat) for lat, lon in points])
    if not shape.is_valid or not shape.is_simple:
        raise PolygonValidationError(
            "The corners form a self-intersecting shape. Capture them in walking order around the boundary."
        )
    area = polygon_area_m2(points)
    if area < MIN_AREA_M2:
        raise PolygonValidationError(f"Plot is only ~{area:.0f} m2; check the GPS captures.")
    if area > MAX_AREA_M2:
        raise PolygonValidationError(f"Plot is ~{area / 1e6:.1f} km2, above the supported maximum.")


def polygon_area_m2(points: list[Point]) -> float:
    lons = [lon for _, lon in points]
    lats = [lat for lat, _ in points]
    area, _ = _GEOD.polygon_area_perimeter(lons, lats)
    return abs(area)


def centroid(points: list[Point]) -> Point:
    c = ShapelyPolygon([(lon, lat) for lat, lon in points]).centroid
    return (c.y, c.x)
