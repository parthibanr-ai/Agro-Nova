from datetime import date, timedelta

from app.providers.base import DailyForecast, ObservedClimate
from app.services import advice, diagnosis, forecast, geocode, knowledge
from app.services.enso import EnsoState

EL_NINO = EnsoState(True, "el_nino", "strong", 1.6, "JJA 2026", "steady")


def _days(rain=0.0, tmax=30.0, tmin=20.0, n=7):
    return [DailyForecast(date.today() + timedelta(days=i), tmax, tmin, rain) for i in range(n)]


def test_knowledge_base_is_consistent():
    remedies = knowledge.load("remedies")["conditions"]
    assert len(remedies) >= 15
    for cid, c in remedies.items():
        assert c["kind"] in {"deficiency", "pest", "disease"}, cid
        assert c["organic_remedies"] and c["chemical_comparison"], cid
    for cid, c in knowledge.crops().items():
        assert c["enso_sensitivity"] in {"high", "medium", "low"}, cid
    for s in knowledge.load("schemes")["schemes"]:
        assert s["country"] in knowledge.countries() and s["url"].startswith("http"), s["id"]


def test_crop_stage_from_sowing_date():
    today = date(2026, 9, 1)
    stage = forecast.crop_stage("rice", today - timedelta(days=70), today)
    assert stage["name"].startswith("Panicle") and stage["water_critical"]
    assert forecast.crop_stage("rice", None) is None


def test_advisories_combine_drought_heat_and_enso():
    observed = ObservedClimate(30, rain_mm=20, rain_normal_mm=100, tmean_c=31)
    stage = {"name": "Panicle initiation-Flowering", "days_after_sowing": 70, "water_critical": True}
    advs = forecast.build_advisories("rice", "IN", stage, observed, _days(rain=0, tmax=37), EL_NINO)
    types = [a["type"] for a in advs]
    assert {"drought", "heat", "irrigation", "enso"} <= set(types)
    assert advs[0]["severity"] == "high"  # sorted most-severe first
    enso = next(a for a in advs if a["type"] == "enso")
    assert "El Nino" in enso["title"] and enso["severity"] == "high"  # rice: high ENSO sensitivity


def test_enso_low_sensitivity_crop_is_low_severity_and_neutral_has_none():
    a = forecast.enso_advisory(EL_NINO, "IN", "millet")
    assert a["severity"] == "low"
    neutral = EnsoState(True, "neutral", "none", 0.1, "JJA 2026", "steady")
    assert forecast.enso_advisory(neutral, "IN", "rice") is None
    assert forecast.enso_advisory(EnsoState(False, "unknown", "unknown", None, None, "unknown"), "IN", "rice") is None


def test_heavy_rain_and_frost():
    advs = forecast.build_advisories("tomato", "IN", None, None, _days(rain=60, tmin=1), EnsoState(False, "unknown", "unknown", None, None, "unknown"))
    assert {"heavy_rain", "frost"} <= {a["type"] for a in advs}


def test_schemes_filtered_by_country_and_category():
    india = advice.matching_schemes("IN", "Maharashtra", "rice", 1.0)
    assert any(s["id"] == "in_pm_kisan" for s in india) and all(s["country"] == "IN" for s in india)
    assert {s["category"] for s in advice.matching_schemes("IN", None, None, None, "water")} == {"water"}
    assert all(s["country"] == "BR" for s in advice.matching_schemes("BR", None, None, None))


def test_livestock_estimate_math():
    est = advice.livestock_estimate(2, 5, milk_price_per_l=50)
    assert est["milk_litres_per_year"] == 2 * 8 * 300
    assert est["dung_tonnes_per_year"] == 7.3  # 2 cows x 10 kg x 365
    assert est["compost_tonnes_per_year"] == 2.9
    assert est["net_milk_margin_per_year"] == 4800 * 50 - 2 * 150 * 365
    assert est["manure_self_sufficiency_pct"] == round(2.92 / 4.0 / 5 * 100)


def test_water_tips_prioritise_el_nino_and_filter_by_crop():
    tips = advice.water_tips("rice", EL_NINO)
    ids = [t["id"] for t in tips]
    assert "sri" in ids and "drought_varieties" in ids
    assert "drip" not in ids  # drip tip is not listed for rice
    assert tips[0]["priority"] >= tips[-1]["priority"]


def test_market_advice_includes_channels_and_schemes():
    m = advice.market_advice("IN", "millet", has_livestock=True)
    assert m["value_addition_ideas"] and any("eNAM" in c for c in m["market_channels"])
    assert {s["id"] for s in m["support_schemes"]} == {"in_aif", "in_pmfme", "in_enam"}
    assert "livestock_value_addition" in m


def test_diagnosis_enriches_with_organic_remedy_and_advantages():
    fake = lambda img, mime, prompt: {"status": "pest", "condition_id": "pest_aphids", "condition_name": "Aphids", "confidence": 0.86,  # noqa: E731
                                        "symptoms_observed": ["curled leaves"], "severity": "medium"}
    out = diagnosis.diagnose(b"x", "image/jpeg", "tomato", "IN", None, model_call=fake)
    assert out["kind"] == "pest" and out["organic_remedies"][0]["name"].startswith("Neem")
    assert out["why_organic"]["advantages"] and out["why_organic"]["vs_chemical"]


def test_diagnosis_low_confidence_and_healthy():
    low = diagnosis.assemble({"status": "deficiency", "condition_id": "def_nitrogen", "confidence": 0.3})
    assert low["status"] == "unclear" and "organic_remedies" not in low
    assert diagnosis.assemble({"status": "healthy", "confidence": 0.9})["status"] == "healthy"
    unknown = diagnosis.assemble({"status": "disease", "condition_id": "other", "condition_name": "Weird", "confidence": 0.8})
    assert "neem" in unknown["message"].lower()


def test_geocode_falls_back_to_google_when_nominatim_finds_nothing(monkeypatch):
    monkeypatch.setattr(geocode, "_search_nominatim", lambda q, limit: [])
    monkeypatch.setattr(geocode, "_search_google", lambda q, limit: [{"display_name": "Achamangalam, Vandavasi, Tamil Nadu, India", "lat": 12.5, "lon": 79.6}])
    results = geocode.search("achamangalam, vandavasi")
    assert results[0]["display_name"] == "Achamangalam, Vandavasi, Tamil Nadu, India"


def test_geocode_skips_google_when_nominatim_already_has_results(monkeypatch):
    monkeypatch.setattr(geocode, "_search_nominatim", lambda q, limit: [{"display_name": "Nashik", "lat": 20.0, "lon": 73.8}])
    monkeypatch.setattr(geocode, "_search_google", lambda q, limit: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert geocode.search("Nashik")[0]["display_name"] == "Nashik"


def test_crop_recommendation_prompt_adds_state_instruction_only_for_india():
    from app.services.crop_recommendation import build_prompt

    enso = EnsoState(True, "neutral", "none", 0.1, "JJA 2026", "steady")
    in_prompt = build_prompt(country_code="IN", state="Punjab", area_ha=2.0, current_crop="rice",
                              soil_values={}, observed=None, enso=enso, today=date(2026, 9, 1))
    assert "Punjab, India" in in_prompt and "Kharif" in in_prompt
    br_prompt = build_prompt(country_code="BR", state="Parana", area_ha=2.0, current_crop="soybean",
                              soil_values={}, observed=None, enso=enso, today=date(2026, 9, 1))
    assert "refine your choice for that state" not in br_prompt and "Safrinha" in br_prompt


def test_crop_recommendation_assemble_resolves_known_crop_and_keeps_unknown():
    from app.services.crop_recommendation import assemble

    raw = {
        "target_season": {"local_name": "Kharif", "months": "Jun-Jul"},
        "recommendations": [
            {"crop_id": "rice", "crop_name": "whatever the model said", "suitability": "high", "reasoning": "wet soil"},
            {"crop_id": "not_a_real_crop", "crop_name": "Foxtail millet", "suitability": "medium", "reasoning": "drought tolerant"},
        ],
        "basis_summary": "test",
    }
    out = assemble(raw)
    assert out["recommendations"][0]["crop_id"] == "rice" and out["recommendations"][0]["crop_name"] == "Rice"
    assert out["recommendations"][1]["crop_id"] is None and out["recommendations"][1]["crop_name"] == "Foxtail millet"
    assert "disclaimer" in out


def test_personalized_advice_prompt_is_grounded_and_kind_specific():
    from app.services.personalized_advice import build_prompt

    enso = EnsoState(True, "el_nino", "moderate", 1.2, "JJA 2026", "steady")
    common = dict(country_code="IN", state="Punjab", area_ha=2.0, current_crop="wheat",
                  soil_values={"ph": {"label": "pH", "value": 6.5, "unit": ""}}, observed=None, enso=enso,
                  has_livestock=True, today=date(2026, 9, 1))

    resilience_prompt = build_prompt("resilience", **common)
    assert "pH: 6.5" in resilience_prompt and "milch livestock" in resilience_prompt
    assert "self-reliant" in resilience_prompt or "resilience" in resilience_prompt

    water_prompt = build_prompt("water", **common)
    assert "water and resource saving" in water_prompt

    market_prompt = build_prompt("market", **common)
    assert "value addition and market" in market_prompt
    # every kind must be grounded in the same underlying plot data, not a generic template
    for p in (resilience_prompt, water_prompt, market_prompt):
        assert "Punjab, India" in p and "pH: 6.5" in p


def test_personalized_advice_assemble_shapes_items_and_adds_disclaimer():
    from app.services.personalized_advice import assemble

    raw = {"summary": "Focus on moisture retention.",
           "items": [{"title": "Mulch now", "detail": "Straw mulch over the root zone.", "why": "Soil moisture is below normal."}]}
    out = assemble(raw)
    assert out["summary"] == "Focus on moisture retention."
    assert out["items"][0] == {"title": "Mulch now", "detail": "Straw mulch over the root zone.", "why": "Soil moisture is below normal."}
    assert "guide" in out["disclaimer"].lower()


def test_geocode_google_fallback_without_key_returns_empty(monkeypatch):
    monkeypatch.setattr(geocode, "_search_nominatim", lambda q, limit: [])
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    assert geocode._search_google("nowhere", 5) == []
    get_settings.cache_clear()
