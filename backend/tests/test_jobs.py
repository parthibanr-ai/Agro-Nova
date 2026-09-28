"""Job-style AI endpoints and the Gemini concurrency cap.

These use a file database: job workers run on their own threads with their own sessions, and the shared in-memory
test engine has a single connection that threads must not use at the same time.
"""

import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db import get_db, migrate
from app.main import app
from app.models import Job
from app.services import crop_recommendation, diagnosis, jobs
from tests.conftest import NASHIK, FakeProvider

H = {"X-Dev-User": "job-farmer"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pg_url = os.environ.get("TEST_POSTGRES_URL")  # optional: run this whole file against a scratch Postgres
    if pg_url:
        from sqlalchemy import text

        from app.db import normalize_database_url

        engine = create_engine(normalize_database_url(pg_url), pool_size=10, max_overflow=10)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}",
                               connect_args={"check_same_thread": False, "timeout": 30})
    migrate(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def db_dep():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_dep
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    from app.api import routes

    from app.services.enso import EnsoState

    monkeypatch.setattr(routes, "get_climate_provider", lambda: FakeProvider())
    monkeypatch.setattr(routes, "get_enso_state", lambda: EnsoState(True, "el_nino", "moderate", 1.2, "JJA 2026", "steady"))
    monkeypatch.setattr(routes.soil, "fetch_soilgrids", lambda lat, lon: {})
    jobs.shutdown()
    client = TestClient(app, headers=H)
    yield type("Env", (), {"client": client, "Session": Session})
    jobs.shutdown()
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _plot(client):
    return client.post("/api/v1/plots", json={"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab",
                                              "corners": NASHIK}).json()["id"]


def _poll(client, url, tries=60):
    for _ in range(tries):
        body = client.get(url).json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("job did not finish")


RESULT = {"target_season": {"local_name": "Rabi"}, "recommendations": [], "basis_summary": "ok"}


# ------------------------------------------------------------------ crop recommendation
def test_a_ready_answer_comes_back_immediately_without_polling(env, monkeypatch):
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    r = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done" and body["result"]["target_season"]["local_name"] == "Rabi"
    assert body["result"]["plot_id"] == pid


def test_a_slow_answer_is_accepted_then_polled(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.1)
    release = threading.Event()

    def slow(prompt):
        release.wait(5)
        return RESULT

    monkeypatch.setattr(crop_recommendation, "call_gemini", slow)
    pid = _plot(env.client)
    r = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    assert r.status_code == 202
    assert r.headers["Location"].startswith("/api/v1/jobs/") and int(r.headers["Retry-After"]) >= 1
    started = r.json()
    assert started["status"] in ("queued", "running") and started["retry_after_s"] >= 1
    assert env.client.get(r.headers["Location"]).json()["status"] in ("queued", "running")
    release.set()
    done = _poll(env.client, r.headers["Location"])
    assert done["status"] == "done" and done["result"]["target_season"]["local_name"] == "Rabi"


def test_the_same_request_while_running_returns_the_running_job(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    release, calls = threading.Event(), []

    def slow(prompt):
        calls.append(1)
        release.wait(5)
        return RESULT

    monkeypatch.setattr(crop_recommendation, "call_gemini", slow)
    pid = _plot(env.client)
    first = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    second = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()  # a double tap
    assert first["job_id"] == second["job_id"]
    release.set()
    _poll(env.client, f"/api/v1/jobs/{first['job_id']}")
    assert len(calls) == 1


def test_failures_look_the_same_as_the_synchronous_endpoint(env, monkeypatch):
    pid = _plot(env.client)
    # Without a Gemini key the direct endpoint says 503; the job endpoint must say the same, at once.
    sync = env.client.get(f"/api/v1/plots/{pid}/crop-recommendation")
    job = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    assert sync.status_code == job.status_code == 503
    assert sync.json()["detail"] == job.json()["detail"]

    def boom(prompt):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(crop_recommendation, "call_gemini", boom)
    assert env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").status_code == 502


def test_a_failed_job_polled_later_reports_its_error(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: (time.sleep(0.4), 1 / 0)[1])
    pid = _plot(env.client)
    r = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    assert r.status_code == 202
    body = _poll(env.client, r.headers["Location"])
    assert body["status"] == "failed" and body["error"]["status"] == 502 and "Try again" in body["error"]["message"]


def test_unknown_plot_is_an_immediate_404_not_a_failed_job(env):
    assert env.client.post("/api/v1/plots/nope/crop-recommendation/jobs").status_code == 404


# ------------------------------------------------------------------ ownership, expiry, lost jobs
def test_jobs_are_private_to_their_owner(env, monkeypatch):
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    job_id = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()["job_id"]
    assert env.client.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert env.client.get(f"/api/v1/jobs/{job_id}", headers={"X-Dev-User": "someone-else"}).status_code == 404
    assert env.client.get("/api/v1/jobs/does-not-exist").status_code == 404


def test_a_job_lost_with_its_instance_is_failed_after_the_lease(env, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    job_id = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()["job_id"]
    with env.Session() as db:  # simulate: the instance died mid-job, long ago
        job = db.get(Job, job_id)
        job.status, job.result = "running", None
        job.created_at = datetime.now(timezone.utc) - timedelta(seconds=get_settings().job_lease_s + 60)
        db.commit()
    body = env.client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "failed" and body["error"]["status"] == 503


def test_expired_jobs_are_purged(env, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    job_id = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()["job_id"]
    with env.Session() as db:
        db.get(Job, job_id).expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert env.client.post("/api/v1/admin/jobs/purge").status_code == 403
    r = env.client.post("/api/v1/admin/jobs/purge", headers={"X-API-Key": "change-me"})
    assert r.json()["purged"] == 1
    with env.Session() as db:
        assert db.scalars(select(Job)).all() == []


# ------------------------------------------------------------------ backpressure
def test_a_full_queue_turns_new_work_away_with_retry_after(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    monkeypatch.setattr(get_settings(), "job_queue_max", 1)
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 100)
    release = threading.Event()
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: (release.wait(5), RESULT)[1])
    a, b = _plot(env.client), env.client.post("/api/v1/plots", json={"name": "Q", "crop": "rice", "country": "IN",
                                                                    "state": "Punjab", "corners": NASHIK}).json()["id"]
    assert env.client.post(f"/api/v1/plots/{a}/crop-recommendation/jobs").status_code == 202
    busy = env.client.post(f"/api/v1/plots/{b}/crop-recommendation/jobs")
    assert busy.status_code == 503 and busy.headers["Retry-After"] == "5"
    release.set()


def test_job_creation_is_rate_limited_like_the_direct_endpoint(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 2)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    codes = [env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ------------------------------------------------------------------ diagnosis
def test_diagnosis_job_uses_the_photo_and_never_stores_it(env, monkeypatch):
    seen = {}

    def fake_diagnose(data, mime, crop, country, notes):
        seen.update(data=data, mime=mime, crop=crop)
        return diagnosis.assemble({"status": "healthy", "confidence": 0.9})

    monkeypatch.setattr(diagnosis, "diagnose", fake_diagnose)
    pid = _plot(env.client)
    files = {"image": ("leaf.jpg", io.BytesIO(b"\xff\xd8leafbytes"), "image/jpeg")}
    r = env.client.post("/api/v1/diagnosis/jobs", files=files, data={"plot_id": pid})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert seen == {"data": b"\xff\xd8leafbytes", "mime": "image/jpeg", "crop": "wheat"}
    with env.Session() as db:
        stored = str(db.scalars(select(Job)).one().params)
    assert "leafbytes" not in stored and "xff" not in stored.lower().replace("\\xff", "")


def test_diagnosis_job_rejects_bad_uploads_at_once(env):
    bad = env.client.post("/api/v1/diagnosis/jobs", files={"image": ("x.txt", io.BytesIO(b"hi"), "text/plain")})
    assert bad.status_code == 415
    big = env.client.post("/api/v1/diagnosis/jobs",
                          files={"image": ("big.jpg", io.BytesIO(b"0" * (8 * 1024 * 1024 + 1)), "image/jpeg")})
    assert big.status_code == 413


def test_the_sync_endpoints_still_work_for_older_apps(env, monkeypatch):
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    assert env.client.get(f"/api/v1/plots/{pid}/crop-recommendation").status_code == 200


# ------------------------------------------------------------------ Gemini concurrency cap
def test_gemini_calls_never_exceed_the_concurrency_cap(monkeypatch):
    from google import genai

    from app.services import gemini_client

    gemini_client.reset_state()
    monkeypatch.setattr(get_settings(), "gemini_max_concurrency", 3)
    monkeypatch.setattr(get_settings(), "gemini_api_key", "k")
    live, peak, lock = [0], [0], threading.Lock()

    class Models:
        def generate_content(self, model, contents, config):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.15)
            with lock:
                live[0] -= 1
            return type("R", (), {"text": '{"ok": true}'})()

    class Client:
        def __init__(self, **kw):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: gemini_client.generate_json(["x"], 0.1), range(12)))
    assert results == [{"ok": True}] * 12
    assert peak[0] == 3
    gemini_client.reset_state()


def test_a_request_that_cannot_get_a_gemini_slot_is_told_it_is_busy(monkeypatch):
    from google import genai

    from app.services import gemini_client

    gemini_client.reset_state()
    monkeypatch.setattr(get_settings(), "gemini_max_concurrency", 1)
    monkeypatch.setattr(get_settings(), "gemini_queue_timeout_s", 0.1)
    monkeypatch.setattr(get_settings(), "gemini_api_key", "k")
    release = threading.Event()

    class Models:
        def generate_content(self, model, contents, config):
            release.wait(5)
            return type("R", (), {"text": '{"ok": true}'})()

    class Client:
        def __init__(self, **kw):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(gemini_client.generate_json, ["x"], 0.1)
        time.sleep(0.1)
        with pytest.raises(gemini_client.GeminiUnavailable, match="busy"):
            gemini_client.generate_json(["y"], 0.1)
        release.set()
        assert first.result() == {"ok": True}
    gemini_client.reset_state()


# ------------------------------------------------------------------ the other AI screens and the forecast
ADVICE = {"summary": "Mulch and irrigate by soil moisture.", "items": [{"title": "t", "detail": "d", "why": "w"}]}


def test_every_slow_screen_has_a_job_endpoint_that_matches_the_direct_one(env, monkeypatch):
    from app.services import personalized_advice

    monkeypatch.setattr(personalized_advice, "call_gemini", lambda p: ADVICE)
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 100)
    pid = _plot(env.client)
    cases = [
        (f"/api/v1/resilience/jobs?plot_id={pid}", f"/api/v1/resilience?plot_id={pid}"),
        (f"/api/v1/water-tips/jobs?plot_id={pid}", f"/api/v1/water-tips?plot_id={pid}"),
        (f"/api/v1/plots/{pid}/market/jobs", f"/api/v1/plots/{pid}/market"),
        (f"/api/v1/plots/{pid}/forecast/jobs?days=5", f"/api/v1/plots/{pid}/forecast?days=5"),
    ]
    for job_url, direct_url in cases:
        r = env.client.post(job_url)
        assert r.status_code == 200, (job_url, r.text)
        body = r.json()
        assert body["status"] == "done"
        assert body["result"] == env.client.get(direct_url).json(), job_url


def test_a_slow_forecast_is_accepted_then_polled(env, monkeypatch):
    from app.api import routes

    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    release = threading.Event()

    class Slow(FakeProvider):
        def forecast(self, lat, lon, days):
            release.wait(5)
            return super().forecast(lat, lon, days)

    monkeypatch.setattr(routes, "get_climate_provider", lambda: Slow())
    pid = _plot(env.client)
    r = env.client.post(f"/api/v1/plots/{pid}/forecast/jobs")
    assert r.status_code == 202 and r.headers["Location"].startswith("/api/v1/jobs/")
    release.set()
    done = _poll(env.client, r.headers["Location"])
    assert done["status"] == "done" and len(done["result"]["daily"]) == 10


def test_job_endpoints_404_for_someone_elses_plot_and_limit_ai_screens(env, monkeypatch):
    pid = _plot(env.client)
    other = {"X-Dev-User": "not-the-owner"}
    for path in (f"/api/v1/plots/{pid}/market/jobs", f"/api/v1/plots/{pid}/forecast/jobs",
                 f"/api/v1/resilience/jobs?plot_id={pid}", f"/api/v1/water-tips/jobs?plot_id={pid}"):
        assert env.client.post(path, headers=other).status_code == 404, path
    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 1)
    assert env.client.post(f"/api/v1/plots/{pid}/market/jobs").status_code == 200
    assert env.client.post(f"/api/v1/plots/{pid}/market/jobs").status_code == 429


# ------------------------------------------------------------------ "your answer is ready" push
def _slow_recommendation(monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    release = threading.Event()
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: (release.wait(5), RESULT)[1])
    return release


def _pushes(monkeypatch):
    from app.services import notifications

    sent = []
    monkeypatch.setattr(notifications, "send_push", lambda token, title, body, data=None: sent.append((token, title, data)) or True)
    return sent


def test_leaving_the_screen_asks_for_a_push_that_is_sent_when_the_job_finishes(env, monkeypatch):
    sent, release = _pushes(monkeypatch), _slow_recommendation(monkeypatch)
    env.client.post("/api/v1/me/fcm-token", json={"token": "tok-1"})
    pid = _plot(env.client)
    job = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    r = env.client.post(f"/api/v1/jobs/{job['job_id']}/notify")
    assert r.status_code == 200 and r.json()["status"] == "will_notify"
    release.set()
    _poll(env.client, f"/api/v1/jobs/{job['job_id']}")
    for _ in range(50):  # the push is sent just after the row is marked done
        if sent:
            break
        time.sleep(0.05)
    assert len(sent) == 1
    token, title, data = sent[0]
    assert token == "tok-1" and "ready" in title and data == {"job_id": job["job_id"], "kind": "crop_recommendation", "plot_id": pid}


def test_no_push_unless_the_app_asked_for_one(env, monkeypatch):
    sent, release = _pushes(monkeypatch), _slow_recommendation(monkeypatch)
    env.client.post("/api/v1/me/fcm-token", json={"token": "tok-1"})
    pid = _plot(env.client)
    job = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    release.set()
    _poll(env.client, f"/api/v1/jobs/{job['job_id']}")
    time.sleep(0.2)
    assert sent == []


def test_asking_for_a_push_after_the_job_finished_is_a_no_op(env, monkeypatch):
    sent = _pushes(monkeypatch)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    env.client.post("/api/v1/me/fcm-token", json={"token": "tok-1"})
    pid = _plot(env.client)
    job = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    assert job["status"] == "done"
    r = env.client.post(f"/api/v1/jobs/{job['job_id']}/notify")
    assert r.json()["status"] == "already_finished"
    time.sleep(0.2)
    assert sent == []


def test_only_the_owner_can_ask_for_a_push(env, monkeypatch):
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    pid = _plot(env.client)
    job = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    assert env.client.post(f"/api/v1/jobs/{job['job_id']}/notify", headers={"X-Dev-User": "x"}).status_code == 404
    assert env.client.post("/api/v1/jobs/nope/notify").status_code == 404
