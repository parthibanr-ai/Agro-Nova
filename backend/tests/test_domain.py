import pytest

from app.core.languages import LANGUAGES, get_language
from app.domain.polygon import PolygonValidationError, polygon_area_m2, validate_plot
from app.services import enso, soil
from app.services.translation import localize_payload

SQUARE = [(19.9975, 73.7898), (19.9975, 73.7908), (19.9985, 73.7908), (19.9985, 73.7898)]
IN_BBOX = (6.0, 68.0, 37.5, 97.5)


def test_valid_plot_and_area():
    validate_plot(SQUARE, IN_BBOX)
    assert 10_000 < polygon_area_m2(SQUARE) < 13_000  # ~1.1 ha


def test_bowtie_rejected():
    bowtie = [SQUARE[0], SQUARE[2], SQUARE[1], SQUARE[3]]
    with pytest.raises(PolygonValidationError, match="self-intersecting"):
        validate_plot(bowtie, IN_BBOX)


def test_swapped_lat_lon_rejected_by_country_bbox():
    swapped = [(lon, lat) for lat, lon in SQUARE]
    with pytest.raises(PolygonValidationError, match="outside the selected country"):
        validate_plot(swapped, IN_BBOX)


def test_too_few_corners_and_tiny_area():
    with pytest.raises(PolygonValidationError):
        validate_plot(SQUARE[:3], IN_BBOX)
    tiny = [(20.0, 73.0), (20.0, 73.000001), (20.000001, 73.000001), (20.000001, 73.0)]
    with pytest.raises(PolygonValidationError, match="only"):
        validate_plot(tiny, IN_BBOX)


def test_language_registry_covers_22_indian_plus_bric():
    indian = {lang.code for lang in LANGUAGES if lang.group == "IN"}
    assert len(indian) == 22
    assert {"pt-BR", "ru", "zh-CN", "hi", "en"} <= {lang.code for lang in LANGUAGES}
    assert get_language("pt").code == "pt-BR" and get_language("zh").code == "zh-CN"
    assert get_language("zh-cn").code == "zh-CN"
    assert get_language("xx").code == "en"


def test_oni_parse_and_classify():
    text = "SEAS YR TOTAL ANOM\nDJF 2026 27.0 -0.4\nJFM 2026 27.4 0.1\nFMA 2026 27.9 0.6\nMAM 2026 28.4 1.2\n"
    state = enso.state_from_rows(enso.parse_oni(text))
    assert state.phase == "el_nino" and state.strength == "moderate" and state.trend == "strengthening"
    assert enso.classify(-2.1) == ("la_nina", "very_strong")
    assert enso.classify(0.2) == ("neutral", "none")


def test_soilgrids_parse_scales_units():
    payload = {"properties": {"layers": [
        {"name": "phh2o", "depths": [{"values": {"mean": 65}}, {"values": {"mean": 67}}]},
        {"name": "soc", "depths": [{"values": {"mean": 80}}]},
    ]}}
    out = soil.parse_soilgrids(payload)
    assert out["phh2o"]["value"] == 6.6 and out["soc"]["value"] == 8.0
    assert any("organic carbon" in n.lower() for n in soil.interpret(out))


def test_localize_is_noop_without_api_key_and_skips_machine_strings(monkeypatch):
    payload = {"title": "Heavy rain expected", "url": "https://x.gov.in", "crop": "rice", "n": 3}
    assert localize_payload(payload, "hi") == payload  # no key -> English preserved
    calls = []
    monkeypatch.setattr("app.services.translation.translate_texts", lambda texts, tgt: calls.append((texts, tgt)) or {t: f"[{tgt}]{t}" for t in texts})
    out = localize_payload(payload, "hi")
    assert calls == [(["Heavy rain expected"], "hi")]
    assert out["url"] == "https://x.gov.in" and out["crop"] == "rice" and out["title"].startswith("[hi]")
    # unsupported-for-MT languages fall back (Bodo -> Hindi)
    calls.clear()
    localize_payload({"t": "Hello there"}, "brx")
    assert calls[0][1] == "hi"
