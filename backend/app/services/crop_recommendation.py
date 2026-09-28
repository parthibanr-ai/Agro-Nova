"""LLM-based next-sowing-season crop recommendation for one plot.

Grounds the recommendation in the same data the rest of AgriN uses - soil (farmer-entered or ISRIC
SoilGrids), observed satellite/climate conditions (NDVI, rainfall vs normal, soil moisture - from Earth
Engine when configured, else Open-Meteo history), the ENSO state, and the country's own local
sowing-season calendar (e.g. Kharif/Rabi/Zaid in India) - then asks Gemini to choose the next local
season and suggest crops for it. Gemini supplies the seasonal/agronomic judgement; the structured data
keeps that judgement grounded instead of generic.
"""

import json
import logging
from datetime import date

from app.core.config import get_settings
from app.core.languages import authoring_language
from app.services import advice_cache, gemini_client
from app.providers.base import ObservedClimate
from app.services import knowledge
from app.services.context import language_instruction, plot_context_block
from app.services.enso import EnsoState
from app.services.translation import LOCALIZED_MARK, localized_marker

logger = logging.getLogger(__name__)


class RecommendationUnavailable(RuntimeError):
    pass


def build_prompt(*, country_code: str, state: str | None, area_ha: float, current_crop: str | None,
                  soil_values: dict, observed: ObservedClimate | None, enso: EnsoState, today: date,
                  lang: str | None = None) -> str:
    crops_summary = {
        cid: {"name": c["name"], "water_mm_per_season": c["season_water_mm"], "heat_stress_c": c["heat_stress_c"],
              "frost_sensitive": c["frost_sensitive"], "drought_tolerance": c["drought_tolerance"],
              "enso_sensitivity": c["enso_sensitivity"]}
        for cid, c in knowledge.crops().items()
    }
    context = plot_context_block(country_code=country_code, state=state, area_ha=area_ha, current_crop=current_crop,
                                  soil_values=soil_values, observed=observed, enso=enso, today=today)

    return (
        "You are an agronomist advising a smallholder farmer on what to grow in the NEXT sowing season on one "
        "specific plot of land, favouring organic/regenerative practice where reasonable.\n"
        f"{context}"
        f"Crops this app has detailed agronomic data for (prefer these when suitable; use their exact id as "
        f"`crop_id`): {json.dumps(crops_summary)}\n"
        "First decide which of the local sowing seasons listed above is the NEXT one starting from today's date. "
        "Then suggest 2 to 4 crops well-suited to that season given the soil and climate data above. You may "
        "suggest a crop outside the list if genuinely better suited to local conditions; then set crop_id to null "
        "and give crop_name in the local/common name used by farmers there.\n"
        "Reply ONLY with JSON: {\"target_season\": {\"local_name\": str, \"months\": str}, "
        "\"recommendations\": [{\"crop_id\": str|null, \"crop_name\": str, \"suitability\": \"high|medium|low\", "
        "\"reasoning\": str, \"water_and_soil_fit\": str, \"risks\": [str]}], \"basis_summary\": str}"
        f"{language_instruction(lang)}"
    )


def call_gemini(prompt: str) -> dict:
    s = get_settings()
    if not s.gemini_api_key and not s.gemini_use_vertex:
        raise RecommendationUnavailable("Set GEMINI_API_KEY (or GEMINI_USE_VERTEX=true with GCP credentials).")
    return gemini_client.generate_json([prompt], temperature=0.4)


def assemble(raw: dict, lang: str | None = None) -> dict:
    known = knowledge.crops()
    authored = authoring_language(lang)
    recs = []
    for r in raw.get("recommendations", []):
        cid = r.get("crop_id")
        recs.append({
            "crop_id": cid if cid in known else None,
            "crop_name": (known[cid]["name"] if cid in known else r.get("crop_name")) or "Unknown crop",
            "suitability": r.get("suitability", "medium"),
            "reasoning": r.get("reasoning", ""),
            "water_and_soil_fit": r.get("water_and_soil_fit", ""),
            "risks": r.get("risks", []),
        })
        if authored is not None:  # crop_name of a known crop is our own English name, so it is still translated
            recs[-1][LOCALIZED_MARK] = localized_marker(authored.code, ["reasoning", "water_and_soil_fit", "risks"])
    out = {
        "target_season": raw.get("target_season", {}),
        "recommendations": recs,
        "basis_summary": raw.get("basis_summary", ""),
        "disclaimer": "AI-assisted suggestion is a guide, not agronomic certification. Confirm with your local "
                      "Krishi Vigyan Kendra / extension officer and check local seed availability before "
                      "committing a season's sowing.",
    }
    if authored is not None:
        out[LOCALIZED_MARK] = localized_marker(authored.code, ["target_season", "basis_summary"])
    return out


def recommend(*, country_code: str, state: str | None, area_ha: float, current_crop: str | None,
              soil_values: dict, observed: ObservedClimate | None, enso: EnsoState,
              today: date | None = None, model_call=None, lang: str | None = None) -> dict:
    today = today or date.today()
    if model_call is None:
        # Production path: round the inputs so farmers in the same state, crop and conditions share one answer.
        area_ha, soil_values, observed = advice_cache.bucket_inputs(area_ha, soil_values, observed)
    prompt = build_prompt(country_code=country_code, state=state, area_ha=area_ha, current_crop=current_crop,
                           soil_values=soil_values, observed=observed, enso=enso, today=today, lang=lang)
    raw = model_call(prompt) if model_call is not None else advice_cache.cached_call(prompt, call_gemini)
    return assemble(raw, lang)
