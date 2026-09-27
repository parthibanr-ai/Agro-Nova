from datetime import date, timedelta

from app.providers.base import DailyForecast, ObservedClimate
from app.services import advice, diagnosis, forecast, knowledge
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
