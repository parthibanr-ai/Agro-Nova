import io

from tests.conftest import NASHIK

PLOT = {"name": "North field", "country": "IN", "state": "Maharashtra", "crop": "rice", "corners": NASHIK}


def _create(client, **over):
    return client.post("/api/v1/plots", json={**PLOT, **over})


def test_auth_required():
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).get("/api/v1/plots").status_code == 401


def test_meta(client):
    assert len(client.get("/api/v1/meta/languages").json()["languages"]) == 26
    assert {c["code"] for c in client.get("/api/v1/meta/countries").json()["countries"]} == {"IN", "BR", "RU", "CN"}


def test_geocode(client, monkeypatch):
    from app.services import geocode

    monkeypatch.setattr(geocode, "search", lambda q, limit=5: [{"display_name": "Nashik, Maharashtra, India", "lat": 19.99, "lon": 73.79}])
    r = client.get("/api/v1/geocode", params={"q": "Nashik"})
    assert r.status_code == 200
    assert r.json()["results"][0]["display_name"] == "Nashik, Maharashtra, India"


def test_plot_capture_four_corners(client):
    r = _create(client)
    assert r.status_code == 201, r.text
    plot = r.json()
    assert 1.0 < plot["area_ha"] < 1.3 and plot["centroid"]["lat"] > 19.99
    assert client.get("/api/v1/plots").json()["plots"][0]["id"] == plot["id"]


def test_plot_validation_errors(client):
    assert _create(client, corners=NASHIK[:3]).status_code == 422
    swapped = [{"lat": c["lon"], "lon": c["lat"]} for c in NASHIK]
    r = _create(client, corners=swapped)
    assert r.status_code == 422 and "outside the selected country" in r.json()["detail"]
    assert _create(client, crop="dragonfruit").status_code == 422


def test_plots_are_private_per_user(client):
    pid = _create(client).json()["id"]
    other = client.__class__(client.app, headers={"X-Dev-User": "farmer-2"})
    assert other.get(f"/api/v1/plots/{pid}").status_code == 404


def test_soil_flow_prompts_then_uses_farmer_data(client):
    pid = _create(client).json()["id"]
    first = client.get(f"/api/v1/plots/{pid}/soil").json()
    assert first["public_data"]["phh2o"]["value"] == 8.6
    assert first["prompt"]["enter_existing_data"] is True and first["prompt"]["how_to_get_soil_tested"]
    assert any("alkaline" in o for o in first["observations"])

    r = client.post(f"/api/v1/plots/{pid}/soil", json={"lat": 19.998, "lon": 73.790, "source": "soil_health_card", "values": {"ph": 6.9, "oc": 0.4}})
    assert r.status_code == 201
    second = client.get(f"/api/v1/plots/{pid}/soil").json()
    assert second["prompt"]["enter_existing_data"] is False
    assert second["farmer_samples"][0]["lat"] == 19.998
    assert second["public_data"] is None
    assert client.post(f"/api/v1/plots/{pid}/soil", json={"lat": 999, "lon": 0, "values": {}}).status_code == 422


def test_forecast_includes_enso_and_advisories(client):
    pid = _create(client, sowing_date="2026-07-20").json()["id"]
    body = client.get(f"/api/v1/plots/{pid}/forecast?days=7").json()
    types = {a["type"] for a in body["advisories"]}
    assert {"drought", "heat", "enso"} <= types
    assert body["enso"]["phase"] == "el_nino" and len(body["daily"]) == 7 and body["growth_stage"]


def test_schemes_water_market_resilience(client):
    pid = _create(client).json()["id"]
    assert any(s["id"] == "in_pm_kisan" for s in client.get(f"/api/v1/schemes?plot_id={pid}").json()["schemes"])
    assert client.get("/api/v1/water-tips").json()["tips"]
    assert client.get(f"/api/v1/plots/{pid}/market").json()["value_addition_ideas"]
    plan = client.get(f"/api/v1/resilience?plot_id={pid}").json()
    assert plan["steps"] and plan["suggested_cows_for_manure_self_sufficiency"] >= 1
    est = client.post("/api/v1/resilience/livestock-estimate", json={"cows": 3, "area_acres": 4}).json()
    assert est["milk_litres_per_year"] == 7200


def test_profile_language_and_country(client):
    assert client.put("/api/v1/me", json={"language": "ta", "state": "Tamil Nadu", "livestock": {"cows": 2}}).json()["language"] == "ta"
    assert client.put("/api/v1/me", json={"country": "ZZ"}).status_code == 422


def test_diagnosis_endpoint(client, monkeypatch):
    from app.services import diagnosis

    monkeypatch.setattr(diagnosis, "call_gemini", lambda *a: {"status": "deficiency", "condition_id": "def_nitrogen", "confidence": 0.9})
    monkeypatch.setattr(diagnosis, "diagnose", lambda *a, **k: diagnosis.assemble({"status": "deficiency", "condition_id": "def_nitrogen", "confidence": 0.9}))
    files = {"image": ("leaf.jpg", io.BytesIO(b"\xff\xd8fake"), "image/jpeg")}
    r = client.post("/api/v1/diagnosis", files=files, data={"crop": "maize"})
    assert r.status_code == 200 and r.json()["organic_remedies"][0]["name"].startswith("Jeevamrut")
    bad = client.post("/api/v1/diagnosis", files={"image": ("x.txt", io.BytesIO(b"hi"), "text/plain")})
    assert bad.status_code == 415


def test_diagnosis_without_gemini_key_is_503(client):
    files = {"image": ("leaf.jpg", io.BytesIO(b"\xff\xd8fake"), "image/jpeg")}
    assert client.post("/api/v1/diagnosis", files=files).status_code == 503


def test_localization_middleware(client, monkeypatch):
    monkeypatch.setattr("app.services.translation.translate_texts", lambda texts, tgt: {t: f"<{tgt}>{t}" for t in texts})
    body = client.get("/api/v1/schemes?lang=hi").json()
    assert body["schemes"][0]["name"].startswith("<hi>") and body["schemes"][0]["url"].startswith("https://")
    assert client.get("/api/v1/schemes?lang=en").json()["schemes"][0]["name"][0] != "<"


def test_admin_dispatch_dry_run(client):
    _create(client)
    client.post("/api/v1/me/fcm-token", json={"token": "tok"})
    assert client.post("/api/v1/admin/notifications/dispatch").status_code == 403
    r = client.post("/api/v1/admin/notifications/dispatch", headers={"X-API-Key": "change-me"})
    digest = r.json()["digests"][0]
    assert {m["topic"] for m in digest["messages"]} >= {"scheme", "weather", "market"} and digest["sent"] == 0
