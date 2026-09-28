"""A simple latitude/longitude grid used to group nearby farms.

Farms in the same ~5.5 km cell share weather, and the same cell id lets the database group and index them by
area (regional digests, cache keys, usage reports) without a spatial extension. It is deliberately a plain grid
with no extra dependency; if finer or equal-area cells are ever needed, H3 ids can replace it in the same column.
"""

CELL_DEG = 0.05  # ~5.5 km north-south; matches the resolution of the rain and weather products we use


def snap(v: float) -> float:
    """Centre of the grid line nearest to `v`."""
    return round(round(v / CELL_DEG) * CELL_DEG, 4)


def cell_id(lat: float, lon: float) -> str:
    """Stable id of the cell containing a point, e.g. '618_1517'. Negative indices are written with an 'm'."""
    def part(v: float) -> str:
        n = round(v / CELL_DEG)
        return f"m{-n}" if n < 0 else str(n)

    return f"{part(lat)}_{part(lon)}"
