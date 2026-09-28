"""ENSO (El Nino / La Nina) state from NOAA CPC's Oceanic Nino Index (ONI).

ONI is a 3-month running mean of Nino3.4 SST anomalies (deg C): >= +0.5 El Nino, <= -0.5 La Nina.
It lags reality by ~1 month, so the trend of the last few seasons is reported as well.
"""

import logging
import time
from dataclasses import asdict, dataclass

import httpx

logger = logging.getLogger(__name__)

ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
_TTL = 24 * 3600
_FAIL_TTL = 300  # after a failed fetch, do not retry for this long (otherwise every request waits on a dead server)
_cache: tuple[float, "EnsoState"] | None = None
_failed_at: float | None = None


@dataclass
class EnsoState:
    available: bool
    phase: str  # "el_nino" | "la_nina" | "neutral" | "unknown"
    strength: str  # "none" | "weak" | "moderate" | "strong" | "very_strong" | "unknown"
    oni: float | None
    season: str | None  # e.g. "JJA 2026"
    trend: str  # "strengthening" | "weakening" | "steady" | "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


def classify(oni: float) -> tuple[str, str]:
    if oni >= 0.5:
        phase = "el_nino"
    elif oni <= -0.5:
        phase = "la_nina"
    else:
        return "neutral", "none"
    a = abs(oni)
    strength = "weak" if a < 1.0 else "moderate" if a < 1.5 else "strong" if a < 2.0 else "very_strong"
    return phase, strength


def parse_oni(text: str) -> list[tuple[str, int, float]]:
    """Parse the CPC ascii file: lines of `SEAS YR TOTAL ANOM`."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1].isdigit():
            try:
                rows.append((parts[0], int(parts[1]), float(parts[3])))
            except ValueError:
                continue
    return rows


def state_from_rows(rows: list[tuple[str, int, float]]) -> EnsoState:
    if not rows:
        return EnsoState(False, "unknown", "unknown", None, None, "unknown")
    seas, year, oni = rows[-1]
    phase, strength = classify(oni)
    trend = "unknown"
    if len(rows) >= 3:
        delta = abs(oni) - abs(rows[-3][2])
        trend = "strengthening" if delta > 0.2 else "weakening" if delta < -0.2 else "steady"
    return EnsoState(True, phase, strength, oni, f"{seas} {year}", trend)


def get_enso_state(force: bool = False) -> EnsoState:
    global _cache, _failed_at
    if not force and _cache and time.time() - _cache[0] < _TTL:
        return _cache[1]
    if not force and _failed_at is not None and time.time() - _failed_at < _FAIL_TTL:
        return _cache[1] if _cache else EnsoState(False, "unknown", "unknown", None, None, "unknown")
    try:
        resp = httpx.get(ONI_URL, timeout=10)
        resp.raise_for_status()
        state = state_from_rows(parse_oni(resp.text))
        if state.available:
            _cache = (time.time(), state)
            _failed_at = None
        return state
    except Exception as exc:  # noqa: BLE001
        logger.warning("ONI fetch failed: %s", exc)
        _failed_at = time.time()
        return _cache[1] if _cache else EnsoState(False, "unknown", "unknown", None, None, "unknown")
