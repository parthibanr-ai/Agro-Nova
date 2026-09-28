"""Start-up safety checks and account deletion."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.core.config import Settings, check_production_settings, production_problems
from app.main import app


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ------------------------------------------------------------------ admin key
def test_dev_mode_allows_the_default_admin_key():
    assert production_problems(_settings(auth_mode="dev")) == []


@pytest.mark.parametrize("key", ["change-me", "", "   ", "short-key"])
def test_firebase_mode_rejects_a_missing_default_or_short_admin_key(key):
    problems = production_problems(_settings(auth_mode="firebase", admin_api_key=key))
    assert len(problems) == 1 and "ADMIN_API_KEY" in problems[0]
    with pytest.raises(RuntimeError, match="Refusing to start"):
        check_production_settings(_settings(auth_mode="firebase", admin_api_key=key))


def test_firebase_mode_accepts_a_strong_admin_key():
    strong = _settings(auth_mode="firebase", admin_api_key="k" * config.MIN_ADMIN_KEY_LENGTH)
    assert production_problems(strong) == []
    check_production_settings(strong)


def test_the_app_will_not_start_with_the_default_key_in_firebase_mode(monkeypatch):
    monkeypatch.setattr(config.get_settings(), "auth_mode", "firebase")
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
        with TestClient(app):
            pass


# ------------------------------------------------------------------ export and erasure
from app.db import SessionLocal  # noqa: E402
from app.models import Job, Plot, SoilSample, User  # noqa: E402
from app.services import account  # noqa: E402
from tests.conftest import NASHIK  # noqa: E402

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _farmer_with_data(client, uid):
    h = {"X-Dev-User": uid}
    client.put("/api/v1/me", json={"language": "hi", "livestock": {"cows": 2}}, headers=h)
    client.post("/api/v1/me/fcm-token", json={"token": f"tok-{uid}"}, headers=h)
    pid = client.post("/api/v1/plots", json={"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab",
                                             "corners": NASHIK}, headers=h).json()["id"]
    client.post(f"/api/v1/plots/{pid}/soil", json={"lat": 20.0, "lon": 73.79, "source": "lab_test",
                                                    "values": {"phh2o": {"value": 7.1}}}, headers=h)
    with SessionLocal() as db:
        db.add(Job(owner_uid=uid, kind="market", fingerprint="f", params={}, status="done", expires_at=FAR_FUTURE))
        db.commit()
    return pid


def test_a_farmer_can_download_everything_stored_about_them(client):
    pid = _farmer_with_data(client, "export-me")
    data = client.get("/api/v1/me/export", headers={"X-Dev-User": "export-me"}).json()
    assert data["profile"]["uid"] == "export-me" and data["profile"]["language"] == "hi"
    assert data["profile"]["has_push_token"] is True and "tok-export-me" not in str(data)
    assert [p["id"] for p in data["plots"]] == [pid]
    assert data["plots"][0]["soil_samples"][0]["values"] == {"phh2o": {"value": 7.1}}


def test_deleting_the_account_needs_explicit_confirmation(client):
    _farmer_with_data(client, "careful")
    h = {"X-Dev-User": "careful"}
    assert client.delete("/api/v1/me", headers=h).status_code == 400
    assert client.delete("/api/v1/me?confirm=false", headers=h).status_code == 400
    assert len(client.get("/api/v1/plots", headers=h).json()["plots"]) == 1


def test_deleting_the_account_erases_everything_and_only_theirs(client):
    _farmer_with_data(client, "leaving")
    keep = _farmer_with_data(client, "staying")
    assert client.delete("/api/v1/me?confirm=true", headers={"X-Dev-User": "leaving"}).status_code == 204
    with SessionLocal() as db:
        assert db.get(User, "leaving") is None
        assert db.query(Plot).filter(Plot.owner_uid == "leaving").count() == 0
        assert db.query(SoilSample).join(Plot, Plot.id == SoilSample.plot_id).filter(Plot.owner_uid == "leaving").count() == 0
        assert db.query(Job).filter(Job.owner_uid == "leaving").count() == 0
        # the other farmer is untouched
        assert db.get(User, "staying").fcm_token == "tok-staying"
        assert db.query(Job).filter(Job.owner_uid == "staying").count() == 1
    assert [p["id"] for p in client.get("/api/v1/plots", headers={"X-Dev-User": "staying"}).json()["plots"]] == [keep]


def test_erasing_also_removes_the_firebase_sign_in_account(monkeypatch):
    calls = []
    monkeypatch.setattr(account, "_delete_firebase_account", lambda uid: calls.append(uid))
    monkeypatch.setattr(config.get_settings(), "auth_mode", "firebase")
    with SessionLocal() as db:
        db.add(User(uid="fb-user", country="IN"))
        db.commit()
        account.erase(db, db.get(User, "fb-user"))
    assert calls == ["fb-user"]


def test_a_firebase_failure_does_not_undo_the_erasure(monkeypatch):
    import sys
    import types

    boom = types.SimpleNamespace(delete_user=lambda uid: (_ for _ in ()).throw(RuntimeError("firebase down")))
    monkeypatch.setitem(sys.modules, "firebase_admin", types.SimpleNamespace(auth=boom))
    monkeypatch.setitem(sys.modules, "firebase_admin.auth", boom)
    monkeypatch.setattr("app.core.auth.ensure_firebase", lambda: None)
    monkeypatch.setattr(config.get_settings(), "auth_mode", "firebase")
    with SessionLocal() as db:
        db.add(User(uid="fb-user-2", country="IN"))
        db.commit()
        account.erase(db, db.get(User, "fb-user-2"))  # must not raise
        assert db.get(User, "fb-user-2") is None


def test_a_job_whose_farmer_was_deleted_meanwhile_is_dropped_quietly():
    """SQLite does not enforce the foreign key, which lets us build the race: the farmer is erased between a job being
    accepted and a worker picking it up."""
    from app.services import jobs

    with SessionLocal() as db:
        db.add(User(uid="ghost", country="IN"))
        job = Job(owner_uid="ghost", kind="market", fingerprint="g", params={}, status="queued", expires_at=FAR_FUTURE)
        db.add(job)
        db.commit()
        jid = job.id
        db.delete(db.get(User, "ghost"))
        db.commit()
    jobs._run(jid, None)  # must neither raise nor run the handler
    with SessionLocal() as db:
        assert db.get(Job, jid).status == "running"  # claimed, then abandoned: the lease will fail it, nobody polls it


# ------------------------------------------------------------------ App Check
@pytest.fixture()
def firebase_client(monkeypatch):
    """The real auth path, with the two Firebase verifications replaced: the ID token 'id-token' is farmer 'fb-1', the
    App Check token 'good' is genuine, anything else is not."""
    from app.core import auth

    monkeypatch.setattr(config.get_settings(), "auth_mode", "firebase")
    monkeypatch.setattr(auth, "_verify_firebase", lambda token: "fb-1")

    def verify(token):
        if token != "good":
            raise ValueError("bad app check token")

    monkeypatch.setattr(auth, "_verify_app_check", verify)
    return TestClient(app, headers={"Authorization": "Bearer id-token"})


def _app_check_count(result):
    from app.core import metrics

    return metrics.APP_CHECK.labels(result)._value.get()


def test_app_check_off_ignores_the_token(firebase_client, monkeypatch):
    monkeypatch.setattr(config.get_settings(), "app_check_mode", "off")
    assert firebase_client.get("/api/v1/me").status_code == 200


def test_monitor_mode_counts_but_lets_everyone_in(firebase_client, monkeypatch):
    monkeypatch.setattr(config.get_settings(), "app_check_mode", "monitor")
    before = {r: _app_check_count(r) for r in ("valid", "missing", "invalid")}
    assert firebase_client.get("/api/v1/me").status_code == 200
    assert firebase_client.get("/api/v1/me", headers={"X-Firebase-AppCheck": "forged"}).status_code == 200
    assert firebase_client.get("/api/v1/me", headers={"X-Firebase-AppCheck": "good"}).status_code == 200
    assert {r: _app_check_count(r) - before[r] for r in before} == {"valid": 1, "missing": 1, "invalid": 1}


def test_enforce_mode_refuses_missing_and_forged_tokens(firebase_client, monkeypatch):
    monkeypatch.setattr(config.get_settings(), "app_check_mode", "enforce")
    missing = firebase_client.get("/api/v1/me")
    assert missing.status_code == 403 and "could not be verified" in missing.json()["detail"]
    assert firebase_client.get("/api/v1/me", headers={"X-Firebase-AppCheck": "forged"}).status_code == 403
    assert firebase_client.get("/api/v1/me", headers={"X-Firebase-AppCheck": "good"}).status_code == 200


def test_enforce_mode_also_protects_the_costly_endpoints_before_any_work(firebase_client, monkeypatch):
    monkeypatch.setattr(config.get_settings(), "app_check_mode", "enforce")
    assert firebase_client.post("/api/v1/plots/x/crop-recommendation/jobs").status_code == 403
    assert firebase_client.get("/api/v1/resilience").status_code == 403


def test_public_endpoints_need_no_app_check(monkeypatch):
    monkeypatch.setattr(config.get_settings(), "app_check_mode", "enforce")
    monkeypatch.setattr(config.get_settings(), "auth_mode", "firebase")
    anonymous = TestClient(app)
    assert anonymous.get("/api/v1/health").status_code == 200
    assert anonymous.get("/api/v1/meta/languages").status_code == 200


def test_a_bad_app_check_mode_is_refused_at_start_up():
    problems = production_problems(_settings(auth_mode="firebase", admin_api_key="k" * 20, app_check_mode="sometimes"))
    assert problems and "APP_CHECK_MODE" in problems[0]
