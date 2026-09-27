"""Plant-photo diagnosis with Gemini, grounded in AgriN's organic-remedy knowledge base.

Gemini identifies the condition (constrained to our condition ids where possible). The remedy, the
preparation steps and the organic-vs-chemical explanation come from the curated knowledge base, not
from the model, so advice stays consistent and organic-first.
"""

import json
import logging

from app.core.config import get_settings
from app.services import knowledge

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.5


class DiagnosisUnavailable(RuntimeError):
    pass


def _kb() -> dict:
    return knowledge.load("remedies")


def build_prompt(crop: str | None, country: str | None, notes: str | None) -> str:
    conditions = {cid: c["name"] for cid, c in _kb()["conditions"].items()}
    return (
        "You are an agronomist assisting smallholder farmers. Examine the plant photo and identify the most likely "
        "nutrient deficiency, insect pest, disease, or that the plant is healthy.\n"
        f"Crop (if known): {crop or 'unknown'}. Country: {country or 'unknown'}. Farmer notes: {notes or 'none'}.\n"
        "Choose `condition_id` from this list when one fits, otherwise use \"other\" or \"healthy\":\n"
        f"{json.dumps(conditions)}\n"
        "Reply ONLY with JSON: {\"status\": \"healthy|deficiency|pest|disease|unclear\", \"condition_id\": str, "
        "\"condition_name\": str, \"confidence\": number 0-1, \"symptoms_observed\": [str], "
        "\"affected_part\": str, \"severity\": \"low|medium|high\", \"follow_up_questions\": [str]}. "
        "Be honest about uncertainty: if the image is blurry or ambiguous use status \"unclear\" and low confidence."
    )


def call_gemini(image: bytes, mime_type: str, prompt: str) -> dict:
    s = get_settings()
    if not s.gemini_api_key and not s.gemini_use_vertex:
        raise DiagnosisUnavailable("Set GEMINI_API_KEY (or GEMINI_USE_VERTEX=true with GCP credentials).")
    from google import genai
    from google.genai import types

    if s.gemini_use_vertex:
        client = genai.Client(vertexai=True, project=s.gee_cloud_project, location=s.gcp_location)
    else:
        client = genai.Client(api_key=s.gemini_api_key)
    resp = client.models.generate_content(
        model=s.gemini_model,
        contents=[types.Part.from_bytes(data=image, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
    )
    return json.loads(resp.text)


def diagnose(image: bytes, mime_type: str, crop: str | None, country: str | None, notes: str | None,
             model_call=call_gemini) -> dict:
    raw = model_call(image, mime_type, build_prompt(crop, country, notes))
    return assemble(raw)


def assemble(raw: dict) -> dict:
    kb = _kb()
    cond = kb["conditions"].get(raw.get("condition_id", ""))
    confidence = float(raw.get("confidence") or 0)
    status = raw.get("status", "unclear")
    result: dict = {
        "status": status,
        "confidence": round(confidence, 2),
        "condition_id": raw.get("condition_id") if cond else None,
        "condition_name": cond["name"] if cond else raw.get("condition_name") or "Unidentified",
        "symptoms_observed": raw.get("symptoms_observed", []),
        "affected_part": raw.get("affected_part"),
        "severity": raw.get("severity", "low"),
        "follow_up_questions": raw.get("follow_up_questions", []),
        "disclaimer": kb["disclaimer"],
    }
    if status == "healthy":
        result["message"] = "The plant looks healthy. Keep monitoring and maintain soil organic matter."
    elif confidence < MIN_CONFIDENCE or status == "unclear":
        result["status"] = "unclear"
        result["message"] = (
            "We are not confident. Retake the photo in daylight, close to the affected leaf (and one of the whole plant), "
            "or show it to your Krishi Vigyan Kendra / extension officer."
        )
    elif cond:
        result["kind"] = cond["kind"]
        result["known_symptoms"] = cond["symptoms"]
        result["organic_remedies"] = cond["organic_remedies"]
        result["prevention"] = cond["preventive"]
        result["why_organic"] = {
            "advantages": kb["organic_advantages"],
            "vs_chemical": cond["chemical_comparison"],
        }
    else:
        result["message"] = (
            "We could not match this to a condition in our organic library. General organic first steps: remove badly affected "
            "leaves, improve airflow and soil health with compost, spray neem oil (5 ml/L), and consult your local extension officer."
        )
        result["why_organic"] = {"advantages": kb["organic_advantages"]}
    return result
