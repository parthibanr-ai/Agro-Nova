"""Shared plot-context summarisation for LLM-grounded advice.

Turns a plot's soil data, observed satellite/climate conditions, ENSO state and country's local sowing
calendar into compact text blocks that any LLM-based advice generator (crop recommendation, resilience,
water-saving, market) can drop straight into its prompt - so every generator grounds its answer in the
same real numbers instead of drifting into generic advice.
"""

import json

from app.providers.base import ObservedClimate
from app.services import knowledge
from app.services.enso import EnsoState


def soil_summary(values: dict) -> str:
    if not values:
        return "No soil data available."
    parts = []
    for key, item in values.items():
        if isinstance(item, dict) and "value" in item:
            parts.append(f"{item.get('label', key)}: {item['value']} {item.get('unit', '')}".strip())
    return "; ".join(parts) if parts else "No soil data available."


def climate_summary(observed: ObservedClimate | None) -> str:
    if observed is None:
        return "Not available."
    bits = []
    if observed.rain_mm is not None and observed.rain_normal_mm:
        bits.append(f"last {observed.window_days} days rainfall {observed.rain_mm:.0f} mm vs normal {observed.rain_normal_mm:.0f} mm")
    if observed.tmean_c is not None:
        bits.append(f"mean temperature {observed.tmean_c:.1f} C")
    if observed.ndvi is not None:
        ndvi_bit = f"satellite NDVI (vegetation greenness) {observed.ndvi:.2f}"
        if observed.ndvi_normal:
            ndvi_bit += f" vs {observed.ndvi_normal:.2f} normal"
        bits.append(ndvi_bit)
    if observed.soil_moisture_pct is not None:
        bits.append(f"satellite soil moisture {observed.soil_moisture_pct:.0f}%")
    if observed.sources:
        bits.append(f"sources: {', '.join(observed.sources)}")
    return "; ".join(bits) if bits else "Not available."


def state_refinement_instruction(country_code: str, state: str | None) -> str:
    if country_code.upper() == "IN" and state:
        return (
            f" This plot is in {state}, India - refine your choice for that state's specific agro-climatic zone, "
            "usual local cropping pattern and typical regional sowing timing (monsoon arrival and season length "
            "both shift by several weeks between Indian states), not a single all-India date."
        )
    return ""


def enso_summary(enso: EnsoState, country_code: str) -> str:
    if not enso.available:
        return "unavailable."
    country = knowledge.country(country_code)
    impact = country.get("enso_impact", {}).get(enso.phase, {}).get("summary", "")
    return f"{enso.phase} ({enso.strength}). {impact}".strip()


def plot_context_block(*, country_code: str, state: str | None, area_ha: float, current_crop: str | None,
                        soil_values: dict, observed: ObservedClimate | None, enso: EnsoState, today) -> str:
    """The common grounding block: local season calendar + this plot's soil/climate/ENSO state."""
    country = knowledge.country(country_code)
    seasons = country.get("sowing_seasons", [])
    state_instruction = state_refinement_instruction(country_code, state)
    return (
        f"Today's date: {today.isoformat()}. Country: {country['name']} ({country_code}). "
        f"State/region: {state or 'unknown'}.{state_instruction}\n"
        f"Plot area: {area_ha:.2f} ha. Current/last crop on this plot: {current_crop or 'unknown'}.\n"
        f"This country's local sowing seasons: {json.dumps(seasons)}\n"
        f"Soil data for this plot: {soil_summary(soil_values)}\n"
        f"Recent satellite/climate observations for this plot: {climate_summary(observed)}\n"
        f"ENSO (El Nino/La Nina) state: {enso_summary(enso, country_code)}\n"
    )
