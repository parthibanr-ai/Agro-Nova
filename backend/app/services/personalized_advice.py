"""LLM-personalised resilient-farming, water-saving and value-addition/market advice for one plot.

Grounds each suggestion in the same soil, satellite/climate and ENSO data (plus the current crop and the
country's local sowing calendar) that crop_recommendation.py uses, so the advice is specific to this farm
right now rather than a generic checklist.
"""

import json
import logging
from datetime import date

from app.core.config import get_settings
from app.providers.base import ObservedClimate
from app.services.context import plot_context_block
from app.services.enso import EnsoState

logger = logging.getLogger(__name__)


class AdviceUnavailable(RuntimeError):
    pass


# kind -> (short label, task instruction)
_KINDS: dict[str, tuple[str, str]] = {
    "resilience": (
        "self-reliant / regenerative resilience",
        "Suggest concrete, hyper-personalised steps this specific farmer can take toward self-sufficiency and "
        "resilience on THIS plot: on-farm inputs (compost/manure, seed-saving, intercropping, livestock "
        "integration), reducing dependence on bought inputs, and buffering the specific climate/ENSO risk shown "
        "above. Use the actual soil and climate numbers given, not generic advice.",
    ),
    "water": (
        "water and resource saving",
        "Suggest concrete, hyper-personalised water- and resource-saving actions for THIS plot's actual soil "
        "moisture, rainfall vs normal, crop and ENSO outlook above (for example irrigation timing/method, "
        "mulching, or scheduling around the forecast rainfall deficit/surplus). Use the actual numbers given, "
        "not generic tips.",
    ),
    "market": (
        "value addition and market",
        "Suggest concrete, hyper-personalised value-addition and market/selling actions for THIS farmer's "
        "current crop and (if relevant) livestock, given the plot's condition and the coming local season above "
        "- for example processing or grading suited to the expected yield/quality signalled by the data, and "
        "timing sales or storage around that season. Use the actual data given, not generic checklist advice.",
    ),
}


def build_prompt(kind: str, *, country_code: str, state: str | None, area_ha: float, current_crop: str | None,
                  soil_values: dict, observed: ObservedClimate | None, enso: EnsoState, has_livestock: bool,
                  today: date) -> str:
    label, instruction = _KINDS[kind]
    context = plot_context_block(country_code=country_code, state=state, area_ha=area_ha, current_crop=current_crop,
                                  soil_values=soil_values, observed=observed, enso=enso, today=today)
    livestock_bit = "The farmer keeps milch livestock (cows/buffaloes).\n" if has_livestock else ""
    return (
        f"You are an agronomist giving {label} advice to a smallholder farmer for ONE specific plot of land, "
        "favouring organic/regenerative practice where reasonable.\n"
        f"{context}{livestock_bit}"
        f"{instruction}\n"
        "Give 3 to 5 items, ordered by priority for this farmer right now. Reply ONLY with JSON: "
        '{"summary": str, "items": [{"title": str, "detail": str, "why": str}]}'
    )


def call_gemini(prompt: str) -> dict:
    s = get_settings()
    if not s.gemini_api_key and not s.gemini_use_vertex:
        raise AdviceUnavailable("Set GEMINI_API_KEY (or GEMINI_USE_VERTEX=true with GCP credentials).")
    from google import genai
    from google.genai import types

    if s.gemini_use_vertex:
        client = genai.Client(vertexai=True, project=s.gee_cloud_project, location=s.gcp_location)
    else:
        client = genai.Client(api_key=s.gemini_api_key)
    resp = client.models.generate_content(
        model=s.gemini_model,
        contents=[prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
    )
    return json.loads(resp.text)


def assemble(raw: dict) -> dict:
    items = [
        {"title": i.get("title", ""), "detail": i.get("detail", ""), "why": i.get("why", "")}
        for i in raw.get("items", [])
    ]
    return {
        "summary": raw.get("summary", ""),
        "items": items,
        "disclaimer": "AI-assisted suggestion is a guide, not agronomic certification. Confirm with your local "
                      "Krishi Vigyan Kendra / extension officer before acting.",
    }


def advise(kind: str, *, country_code: str, state: str | None, area_ha: float, current_crop: str | None,
           soil_values: dict, observed: ObservedClimate | None, enso: EnsoState, has_livestock: bool = False,
           today: date | None = None, model_call=None) -> dict:
    if kind not in _KINDS:
        raise ValueError(f"Unknown advice kind '{kind}'")
    today = today or date.today()
    prompt = build_prompt(kind, country_code=country_code, state=state, area_ha=area_ha, current_crop=current_crop,
                           soil_values=soil_values, observed=observed, enso=enso, has_livestock=has_livestock,
                           today=today)
    raw = (model_call or call_gemini)(prompt)
    return assemble(raw)
