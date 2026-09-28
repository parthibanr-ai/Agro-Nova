"""Gemini writes advice directly in the farmer's language, and the translation layer leaves that text alone."""

import json
from datetime import date

import pytest

from app.core.config import get_settings
from app.core.languages import authoring_language
from app.providers.base import ObservedClimate
from app.services import crop_recommendation, personalized_advice, translation
from app.services.enso import EnsoState
from app.services.translation import LOCALIZED_MARK, localize_payload, localized_marker
from tests.conftest import NASHIK

ENSO = EnsoState(True, "el_nino", "moderate", 1.2, "JJA 2026", "steady")
HINDI = {"summary": "मिट्टी में नमी रखें।", "items": [{"title": "मल्चिंग", "detail": "पुआल बिछाएँ", "why": "नमी बचती है"}]}


class FakeTranslate:
    """Cloud Translation stand-in: marks what it translated as <text> and counts the characters it was asked for."""

    requests: list[list[str]] = []

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def post(self, url, params, json):
        FakeTranslate.requests.append(list(json["q"]))
        body = {"data": {"translations": [{"translatedText": f"<{t}>"} for t in json["q"]]}}
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()


@pytest.fixture()
def translating(monkeypatch):
    FakeTranslate.requests = []
    monkeypatch.setattr(get_settings(), "translate_api_key", "k")
    monkeypatch.setattr(translation.httpx, "Client", FakeTranslate)
    return FakeTranslate


def _translated_texts():
    return {t for batch in FakeTranslate.requests for t in batch}


# ------------------------------------------------------------------ which languages
def test_gemini_writes_directly_only_in_reviewed_languages():
    for code in ("hi", "ta", "bn", "te", "mr", "gu", "kn", "ml", "pa", "pt-BR", "ru", "zh-CN"):
        assert authoring_language(code) is not None, code
    for code in (None, "", "en", "doi", "kok", "mai", "mni", "sd", "brx", "ks", "sa", "sat", "xx"):
        assert authoring_language(code) is None, code
    assert authoring_language("hi-IN").code == "hi"  # region-tagged codes resolve


def _prompt(lang):
    return personalized_advice.build_prompt(
        "water", country_code="IN", state="Punjab", area_ha=1.0, current_crop="wheat", soil_values={},
        observed=ObservedClimate(30, rain_mm=10), enso=ENSO, has_livestock=False, today=date(2026, 9, 28), lang=lang)


def test_the_prompt_asks_for_the_language_only_when_it_should():
    hindi = _prompt("hi")
    assert "Hindi" in hindi and "हिन्दी" in hindi and "crop_id" in hindi
    assert "Write every human-readable" not in _prompt("en")
    assert "Write every human-readable" not in _prompt(None)
    assert "Write every human-readable" not in _prompt("doi")  # not reviewed: English, then translated as before
    assert "Tamil" in _prompt("ta")


def test_the_crop_prompt_also_asks_for_the_language():
    kw = dict(country_code="IN", state="Punjab", area_ha=1.0, current_crop="wheat", soil_values={}, observed=None,
              enso=ENSO, today=date(2026, 9, 28))
    assert "Hindi" in crop_recommendation.build_prompt(**kw, lang="hi")
    assert "Hindi" not in crop_recommendation.build_prompt(**kw, lang="en")


# ------------------------------------------------------------------ what is marked, and what is translated
def test_advice_written_in_hindi_is_marked_and_english_is_not():
    hi = personalized_advice.assemble(HINDI, "hi")
    assert hi[LOCALIZED_MARK] == {"lang": "hi", "keys": ["summary", "items"]}
    assert LOCALIZED_MARK not in personalized_advice.assemble(HINDI, "en")
    assert LOCALIZED_MARK not in personalized_advice.assemble(HINDI, "doi")


def test_only_the_fixed_text_is_sent_for_translation(translating):
    out = localize_payload({"personalized": personalized_advice.assemble(HINDI, "hi"), "tips": ["Mulch the soil"]}, "hi")
    sent = _translated_texts()
    assert not any("नमी" in t or "मल्चिंग" in t for t in sent), sent  # Gemini's Hindi never reaches the paid API
    assert any("AI-assisted" in t for t in sent) and "Mulch the soil" in sent  # fixed English text still is
    assert out["personalized"]["summary"] == HINDI["summary"] and out["personalized"]["items"] == HINDI["items"]
    assert out["personalized"]["disclaimer"].startswith("<AI-assisted")
    assert out["tips"] == ["<Mulch the soil>"]
    assert LOCALIZED_MARK not in json.dumps(out)


def test_a_marker_for_another_language_is_ignored_but_still_removed(translating):
    payload = {"summary": "Keep the soil moist", LOCALIZED_MARK: localized_marker("hi", ["summary"])}
    out = localize_payload(payload, "ta")  # e.g. the farmer switched language while a job was running
    assert out == {"summary": "<Keep the soil moist>"}


def test_markers_never_leak_to_english_readers_or_when_translation_is_off():
    payload = {"a": {"x": "text", LOCALIZED_MARK: localized_marker("hi", ["x"])}}
    assert localize_payload(payload, "en") == {"a": {"x": "text"}}
    assert localize_payload(payload, "hi") == {"a": {"x": "text"}}  # no TRANSLATE_API_KEY


# ------------------------------------------------------------------ end to end through the API
@pytest.fixture()
def gemini(monkeypatch):
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return HINDI if "Hindi" in prompt else {"summary": "Keep the soil moist.", "items": []}

    monkeypatch.setattr(personalized_advice, "call_gemini", fake)
    return prompts


def _plot(client, uid):
    h = {"X-Dev-User": uid}
    return client.post("/api/v1/plots", json={"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab",
                                              "corners": NASHIK}, headers=h).json()["id"], h


def test_a_hindi_farmer_gets_hindi_advice_without_paying_to_translate_it(client, gemini, translating):
    pid, h = _plot(client, "hindi-farmer")
    body = client.get(f"/api/v1/water-tips?plot_id={pid}&lang=hi", headers=h).json()
    assert body["personalized"]["summary"] == HINDI["summary"]
    assert body["personalized"]["disclaimer"].startswith("<")  # the fixed disclaimer is translated
    assert LOCALIZED_MARK not in json.dumps(body)
    assert not any("नमी" in t for t in _translated_texts())
    assert "Hindi" in gemini[0]


def test_farmers_writing_the_same_language_share_one_gemini_call_and_other_languages_do_not(client, gemini, translating):
    ids = [_plot(client, f"f{i}") for i in range(3)]
    for pid, h in ids[:2]:
        client.get(f"/api/v1/water-tips?plot_id={pid}&lang=hi", headers=h)
    assert len(gemini) == 1  # same state, crop and conditions, same language: one call
    pid, h = ids[2]
    english = client.get(f"/api/v1/water-tips?plot_id={pid}", headers=h).json()
    assert len(gemini) == 2 and "Hindi" not in gemini[1]
    assert english["personalized"]["summary"] == "Keep the soil moist."


def test_a_language_not_yet_reviewed_still_uses_english_then_translation(client, gemini, translating):
    pid, h = _plot(client, "dogri-farmer")
    body = client.get(f"/api/v1/water-tips?plot_id={pid}&lang=doi", headers=h).json()
    assert body["personalized"]["summary"] == "<Keep the soil moist.>"
    assert "Write every human-readable" not in gemini[0]


def test_the_language_reaches_the_job_workers_and_is_part_of_the_job(client, gemini, translating):
    pid, h = _plot(client, "job-hindi")
    hi = client.post(f"/api/v1/water-tips/jobs?plot_id={pid}&lang=hi", headers=h)
    assert hi.status_code == 200
    assert hi.json()["result"]["personalized"]["summary"] == HINDI["summary"]
    assert LOCALIZED_MARK not in hi.text
    assert "Hindi" in gemini[0]
    en = client.post(f"/api/v1/water-tips/jobs?plot_id={pid}", headers=h)  # same farmer, other language: a new job
    assert en.json()["job_id"] != hi.json()["job_id"]
    assert en.json()["result"]["personalized"]["summary"] == "Keep the soil moist."


def test_crop_recommendation_keeps_translating_our_own_crop_names(client, translating, monkeypatch):
    raw = {"target_season": {"local_name": "रबी", "months": "अक्टूबर-मार्च"},
           "recommendations": [{"crop_id": "wheat", "crop_name": "गेहूँ", "suitability": "high",
                                "reasoning": "ठंडा मौसम अनुकूल है", "water_and_soil_fit": "अच्छा", "risks": ["पाला"]}],
           "basis_summary": "मिट्टी और मौसम के आधार पर"}
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: raw)
    pid, h = _plot(client, "crop-hindi")
    body = client.get(f"/api/v1/plots/{pid}/crop-recommendation?lang=hi", headers=h).json()
    rec = body["recommendations"][0]
    assert rec["reasoning"] == "ठंडा मौसम अनुकूल है" and rec["risks"] == ["पाला"]
    assert body["basis_summary"] == "मिट्टी और मौसम के आधार पर"
    assert rec["crop_name"].startswith("<")  # "Wheat" comes from our own crop list, in English
    assert not any("मौसम" in t for t in _translated_texts())
