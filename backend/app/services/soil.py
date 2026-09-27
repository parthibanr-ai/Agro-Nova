"""Soil data: farmer-provided samples first, then public global/national datasets.

Flow (see routes): if the plot has a farmer-supplied sample (e.g. Soil Health Card) we use it; else
we query ISRIC SoilGrids (250 m, global, public). Either way the response carries a `prompt` that asks
the farmer to enter government soil data they already hold, or explains how to obtain it locally.
"""

import logging

import httpx

from app.services import knowledge

logger = logging.getLogger(__name__)

SOILGRIDS = "https://rest.isric.org/soilgrids/v2.0/properties/query"
PROPERTIES = ["phh2o", "soc", "nitrogen", "clay", "sand", "silt", "cec"]
# SoilGrids stores scaled ints; d_factor converts to conventional units.
_CONVERT = {
    "phh2o": ("pH (water)", 10, ""),
    "soc": ("Soil organic carbon", 10, "g/kg"),
    "nitrogen": ("Total nitrogen", 100, "g/kg"),
    "clay": ("Clay", 10, "%"),
    "sand": ("Sand", 10, "%"),
    "silt": ("Silt", 10, "%"),
    "cec": ("Cation exchange capacity", 10, "cmol(c)/kg"),
}


def fetch_soilgrids(lat: float, lon: float) -> dict | None:
    params = [("lat", lat), ("lon", lon), ("depth", "0-5cm"), ("depth", "5-15cm"), ("value", "mean")]
    params += [("property", p) for p in PROPERTIES]
    try:
        resp = httpx.get(SOILGRIDS, params=params, timeout=30)
        resp.raise_for_status()
        return parse_soilgrids(resp.json())
    except Exception as exc:  # noqa: BLE001
        logger.warning("SoilGrids unavailable: %s", exc)
        return None


def parse_soilgrids(payload: dict) -> dict:
    out: dict = {}
    for layer in payload.get("properties", {}).get("layers", []):
        name = layer["name"]
        if name not in _CONVERT:
            continue
        label, factor, unit = _CONVERT[name]
        vals = [d["values"].get("mean") for d in layer["depths"] if d["values"].get("mean") is not None]
        if vals:
            out[name] = {"label": label, "value": round(sum(vals) / len(vals) / factor, 2), "unit": unit, "depth": "0-15 cm"}
    return out


def interpret(values: dict) -> list[str]:
    """Plain-language observations from soil values (any source)."""
    notes = []
    ph = _v(values, "phh2o") or _v(values, "ph")
    if ph is not None:
        if ph < 5.5:
            notes.append(f"Soil is strongly acidic (pH {ph}); add lime/dolomite and compost. Nutrients like phosphorus become locked.")
        elif ph > 8.3:
            notes.append(f"Soil is alkaline (pH {ph}); add compost, gypsum or green manure. Iron and zinc are often deficient.")
        else:
            notes.append(f"pH {ph} is in a workable range for most crops.")
    soc = _v(values, "soc")
    if soc is not None and soc < 10:
        notes.append("Organic carbon is low: regular compost/FYM, mulching and green manure will improve fertility and water-holding.")
    oc = _v(values, "oc")  # % from lab reports
    if oc is not None and oc < 0.5:
        notes.append("Organic carbon is low (<0.5%): build it up with compost, FYM and cover crops.")
    return notes


def _v(values: dict, key: str) -> float | None:
    item = values.get(key)
    if isinstance(item, dict):
        item = item.get("value")
    return item if isinstance(item, (int, float)) else None


def acquisition_prompt(country_code: str, has_farmer_data: bool) -> dict:
    c = knowledge.country(country_code)
    if has_farmer_data:
        message = "We are using the soil data you entered. Add a new sample any time - for example after each season - and tag where in the plot it was taken."
    else:
        message = (
            "The values below are modelled from public satellite/global datasets (about 250 m resolution) and can differ from your field. "
            "If you already hold a government soil report (for example a Soil Health Card in India), please enter it. "
            "If not, follow the steps below to get your soil tested by the local government initiative."
        )
    return {
        "message": message,
        "enter_existing_data": not has_farmer_data,
        "how_to_get_soil_tested": c["soil_testing_guidance"],
        "official_sources": c["agencies"]["soil"],
        "capture_location_hint": "When you enter a sample, we record its latitude/longitude (tap 'Use my GPS' at the sampling spot).",
    }
