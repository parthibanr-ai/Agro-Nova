"""LLM-personalised resilient-farming, water-saving and value-addition/market advice for one plot.

Grounds each suggestion in the same soil, satellite/climate and ENSO data (plus the current crop and the
country's local sowing calendar) that crop_recommendation.py uses, so the advice is specific to this farm
right now rather than a generic checklist.
"""

import logging
from datetime import date

from app.core.config import get_settings
from app.core.languages import authoring_language
from app.services import advice_cache, gemini_client
from app.providers.base import ObservedClimate
from app.services.context import language_instruction, plot_context_block
from app.services.enso import EnsoState
from app.services.translation import LOCALIZED_MARK, localized_marker

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
                  today: date, lang: str | None = None) -> str:
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
        f"{language_instruction(lang)}"
    )


def call_gemini(prompt: str) -> dict:
    s = get_settings()
    if not gemini_client.any_vendor_configured(s):
        raise AdviceUnavailable("Set OPENAI_API_KEY, ANTHROPIC_API_KEY or GEMINI_API_KEY (or GEMINI_USE_VERTEX=true).")
    return gemini_client.generate_json([prompt], temperature=0.4)


def assemble(raw: dict, lang: str | None = None) -> dict:
    items = [
        {"title": i.get("title", ""), "detail": i.get("detail", ""), "why": i.get("why", "")}
        for i in raw.get("items", [])
    ]
    out = {
        "summary": raw.get("summary", ""),
        "items": items,
        "disclaimer": "AI-assisted suggestion is a guide, not agronomic certification. Confirm with your local "
                      "Krishi Vigyan Kendra / extension officer before acting.",
    }
    authored = authoring_language(lang)
    if authored is not None:  # Gemini wrote these two in the farmer's language; the fixed disclaimer is translated
        out[LOCALIZED_MARK] = localized_marker(authored.code, ["summary", "items"])
    return out


def advise(kind: str, *, country_code: str, state: str | None, area_ha: float, current_crop: str | None,
           soil_values: dict, observed: ObservedClimate | None, enso: EnsoState, has_livestock: bool = False,
           today: date | None = None, model_call=None, lang: str | None = None) -> dict:
    if kind not in _KINDS:
        raise ValueError(f"Unknown advice kind '{kind}'")
    today = today or date.today()
    if model_call is None:
        # Production path: round the inputs so farmers in the same state, crop and conditions share one answer.
        area_ha, soil_values, observed = advice_cache.bucket_inputs(area_ha, soil_values, observed)
    prompt = build_prompt(kind, country_code=country_code, state=state, area_ha=area_ha, current_crop=current_crop,
                           soil_values=soil_values, observed=observed, enso=enso, has_livestock=has_livestock,
                           today=today, lang=lang)
    raw = model_call(prompt) if model_call is not None else advice_cache.cached_call(prompt, call_gemini)
    return assemble(raw, lang)
