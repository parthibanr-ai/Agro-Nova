"""Privacy notice, consent records, enforcement, offline-safe writes, retention and the push deep-link data."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import ConsentEvent, Plot, SoilSample, User
from app.services import consent
from tests.conftest import NASHIK

V = consent.version()
ALL = {"service": True, "ai": True, "notifications": True}


def _plot_body(**extra):
    return {"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab", "corners": NASHIK, **extra}


def _agree(client, **over):
    return client.put("/api/v1/me/consent", json={"notice_version": V, "purposes": {**ALL, **over}})


@pytest.fixture(autouse=True)
def _clean_farmers():
    """The test database is shared, so start each test without these farmers (and their consent history)."""
    from app.services import account

    def wipe():
        for uid in ("farmer-1", "farmer-2", "active"):
            with SessionLocal() as db:
                u = db.get(User, uid)
                if u is not None:
                    account.erase(db, u)
                db.query(ConsentEvent).filter_by(uid=uid).delete()
                db.commit()

    wipe()
    yield
    wipe()


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setattr(get_settings(), "consent_mode", "enforce")


# ------------------------------------------------------------------ notice
def test_notice_is_public_versioned_and_lists_purposes(client):
    r = TestClient(app).get("/api/v1/consent/notice")  # no sign-in
    assert r.status_code == 200
    n = r.json()
    assert n["version"] == V and n["intro"] and n["sections"]
    assert [p["id"] for p in n["purposes"]] == ["service", "ai", "notifications"]
    assert n["purposes"][0]["required"] is True and n["purposes"][1]["required"] is False


def test_notice_states_this_deployments_retention_period_and_contacts(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "retention_days", 400)
    monkeypatch.setattr(s, "grievance_officer_email", "dpo@example.org")
    n = client.get("/api/v1/consent/notice").json()
    assert "400 days" in " ".join(sec["body"] for sec in n["sections"])
    assert "{retention_days}" not in str(n)
    assert n["grievance_officer"]["email"] == "dpo@example.org"


# ------------------------------------------------------------------ recording consent
def test_a_new_farmer_needs_to_consent_and_the_choice_is_recorded(client):
    st = client.get("/api/v1/me/consent").json()
    assert st["needs_consent"] is True and st["accepted_version"] is None and st["notice_version"] == V
    st = _agree(client, notifications=False).json()
    assert st["needs_consent"] is False and st["purposes"] == {"service": True, "ai": True, "notifications": False}
    assert client.get("/api/v1/me/consent").json()["accepted_version"] == V


def test_the_required_purpose_cannot_be_refused_and_every_choice_must_be_answered(client):
    assert _agree(client, service=False).status_code == 422
    r = client.put("/api/v1/me/consent", json={"notice_version": V, "purposes": {"service": True}})
    assert r.status_code == 422 and "missing" in r.json()["detail"]
    r = client.put("/api/v1/me/consent", json={"notice_version": V, "purposes": {**ALL, "ads": True}})
    assert r.status_code == 422


def test_an_answer_to_an_old_notice_is_refused(client):
    r = client.put("/api/v1/me/consent", json={"notice_version": "1999-01", "purposes": ALL})
    assert r.status_code == 409 and "updated" in r.json()["detail"]


def test_every_change_is_logged_and_repeating_a_choice_logs_nothing(client):
    _agree(client)
    _agree(client)  # an offline retry
    _agree(client, notifications=False)  # withdrawal
    with SessionLocal() as db:
        ev = [(e.purpose, e.granted) for e in db.query(ConsentEvent).filter_by(uid="farmer-1").order_by(ConsentEvent.id)]
    assert ev == [("service", True), ("ai", True), ("notifications", True), ("notifications", False)]


def test_withdrawing_notifications_drops_the_device_token(client):
    _agree(client)
    client.post("/api/v1/me/fcm-token", json={"token": "tok"})
    with SessionLocal() as db:
        assert db.get(User, "farmer-1").fcm_token == "tok"
    _agree(client, notifications=False)
    with SessionLocal() as db:
        assert db.get(User, "farmer-1").fcm_token is None


def test_a_new_notice_version_asks_again(client, monkeypatch):
    _agree(client)
    monkeypatch.setattr(consent, "version", lambda: "2099-01-1")
    st = client.get("/api/v1/me/consent").json()
    assert st["needs_consent"] is True and st["accepted_version"] == V and st["notice_version"] == "2099-01-1"


# ------------------------------------------------------------------ enforcement
def test_enforce_mode_refuses_plots_ai_and_push_until_consent(client, enforce):
    assert client.post("/api/v1/plots", json=_plot_body()).status_code == 403
    r = client.post("/api/v1/plots", json=_plot_body())
    assert r.json()["detail"] == "Consent required: service"
    assert client.get("/api/v1/resilience").json()["detail"] == "Consent required: ai"
    assert client.post("/api/v1/resilience/jobs").status_code == 403
    assert client.post("/api/v1/me/fcm-token", json={"token": "t"}).json()["detail"] == "Consent required: notifications"
    assert client.get("/api/v1/plots").status_code == 200, "reading is never blocked"
    assert client.get("/api/v1/schemes").status_code == 200


def test_after_consent_enforce_mode_lets_requests_through_per_purpose(client, enforce):
    _agree(client, ai=False, notifications=False)
    assert client.post("/api/v1/plots", json=_plot_body()).status_code == 201
    assert client.get("/api/v1/resilience").status_code == 403, "AI not agreed"
    assert client.post("/api/v1/me/fcm-token", json={"token": "t"}).status_code == 403
    _agree(client)
    assert client.get("/api/v1/resilience").status_code == 200
    assert client.post("/api/v1/me/fcm-token", json={"token": "t"}).status_code == 204


def test_refusal_for_missing_consent_does_not_use_the_rate_limit(client, enforce, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 1)
    for _ in range(3):
        assert client.get("/api/v1/resilience").status_code == 403
    _agree(client)
    assert client.get("/api/v1/resilience").status_code == 200


def test_monitor_and_off_modes_count_or_ignore_but_never_refuse(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "consent_mode", "monitor")
    assert client.post("/api/v1/plots", json=_plot_body()).status_code == 201
    monkeypatch.setattr(get_settings(), "consent_mode", "off")
    assert client.post("/api/v1/plots", json=_plot_body()).status_code == 201


# ------------------------------------------------------------------ export and erasure
def test_export_includes_consent_history_and_erasure_removes_it(client):
    _agree(client)
    assert len(client.get("/api/v1/me/export").json()["consent"]["history"]) == 3
    assert client.delete("/api/v1/me?confirm=true").status_code == 204
    with SessionLocal() as db:
        assert db.query(ConsentEvent).filter_by(uid="farmer-1").count() == 0


# ------------------------------------------------------------------ offline-safe writes
def test_saving_a_plot_twice_with_the_same_client_ref_makes_one_plot(client):
    a = client.post("/api/v1/plots", json=_plot_body(client_ref="phone-1"))
    b = client.post("/api/v1/plots", json=_plot_body(client_ref="phone-1", name="renamed"))
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] == b.json()["id"] and b.json()["client_ref"] == "phone-1"
    assert len(client.get("/api/v1/plots").json()["plots"]) == 1
    other = client.post("/api/v1/plots", json=_plot_body(client_ref="phone-2"))
    assert other.json()["id"] != a.json()["id"]


def test_client_refs_are_private_to_each_farmer(client):
    client.post("/api/v1/plots", json=_plot_body(client_ref="same"))
    other = TestClient(app, headers={"X-Dev-User": "farmer-2"})
    r = other.post("/api/v1/plots", json=_plot_body(client_ref="same"))
    assert r.status_code == 201 and len(other.get("/api/v1/plots").json()["plots"]) == 1


def test_a_soil_sample_retry_returns_the_stored_sample(client):
    pid = client.post("/api/v1/plots", json=_plot_body()).json()["id"]
    body = {"lat": 19.99, "lon": 73.79, "source": "lab_test", "values": {"ph": 6.8}, "client_ref": "s-1"}
    a = client.post(f"/api/v1/plots/{pid}/soil", json=body)
    b = client.post(f"/api/v1/plots/{pid}/soil", json=body)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    with SessionLocal() as db:
        assert db.query(SoilSample).filter_by(plot_id=pid).count() == 1


def test_without_a_client_ref_nothing_is_deduplicated(client):
    client.post("/api/v1/plots", json=_plot_body())
    client.post("/api/v1/plots", json=_plot_body())
    assert len(client.get("/api/v1/plots").json()["plots"]) == 2


# ------------------------------------------------------------------ retention
def _age(uid, days):
    with SessionLocal() as db:
        u = db.get(User, uid)
        u.last_seen_at = datetime.now(timezone.utc) - timedelta(days=days)
        db.commit()


def test_last_seen_is_recorded_on_first_use(client):
    client.get("/api/v1/me")
    with SessionLocal() as db:
        assert db.get(User, "farmer-1").last_seen_at is not None


def test_retention_reports_by_default_and_erases_only_with_apply(client):
    from app.batch import retention

    client.post("/api/v1/plots", json=_plot_body())
    TestClient(app, headers={"X-Dev-User": "active"}).get("/api/v1/me")
    _age("farmer-1", 800)
    dry = retention.run(days=730)
    assert dry["due"] == 1 and dry["erased"] == 0 and dry["applied"] is False
    with SessionLocal() as db:
        assert db.get(User, "farmer-1") is not None
    done = retention.run(days=730, apply=True)
    assert done["erased"] == 1
    with SessionLocal() as db:
        assert db.get(User, "farmer-1") is None and db.get(User, "active") is not None
        assert db.query(Plot).filter_by(owner_uid="farmer-1").count() == 0


def test_retention_refuses_a_dangerously_short_period():
    from app.batch import retention

    with pytest.raises(ValueError):
        retention.run(days=3, apply=True)


def test_an_invalid_consent_mode_stops_a_production_start(monkeypatch):
    from app.core import config

    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "firebase")
    monkeypatch.setattr(s, "admin_api_key", "a-long-random-key-for-the-test-1234")
    monkeypatch.setattr(s, "consent_mode", "sometimes")
    assert any("CONSENT_MODE" in p for p in config.production_problems(s))


# ------------------------------------------------------------------ push deep link data
def test_the_ready_push_names_the_plot_only_when_the_job_has_one(monkeypatch):
    from app.services import jobs, notifications

    sent = []
    monkeypatch.setattr(notifications, "send_push", lambda token, title, body, data=None: sent.append(data))
    user = User(uid="u", fcm_token="tok", language="en")
    jobs._notify(user, "j1", "crop_recommendation", "plot-9")
    jobs._notify(user, "j2", "resilience", None)
    assert sent == [{"job_id": "j1", "kind": "crop_recommendation", "plot_id": "plot-9"},
                    {"job_id": "j2", "kind": "resilience"}]
