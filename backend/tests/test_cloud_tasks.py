"""Cloud Tasks as the background-work backend: the adapter, the task endpoint's authentication, and AI jobs that run
on another instance with their photo parked in Cloud Storage.

Google's services are replaced by fakes (no network, no credentials): a queue that records the tasks it is given and
can "deliver" them the way Cloud Tasks does (an HTTP POST with an OIDC bearer token), and an in-memory payload store.
The wiring to the real client libraries is therefore checked only for its shape; it needs a staging deployment to be
proven end to end (see docs/OPERATIONS.md).
"""

import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import AlreadyExists
from sqlalchemy import select

from app.core import auth, config
from app.core.config import Settings, configuration_problems, get_settings
from app.main import app
from app.models import Job
from app.services import crop_recommendation, diagnosis, jobs, payload_store, tasks
from tests.test_jobs import RESULT, _plot, env  # noqa: F401 - `env` is a fixture (file database, fake providers)

SA = "queue-caller@agrin-prod.iam.gserviceaccount.com"
TARGET = "https://agrin-api-abc.a.run.app"


class FakeQueue:
    """Stands in for google.cloud.tasks_v2.CloudTasksClient."""

    def __init__(self):
        self.tasks = []
        self.fail_with = None
        self.after_create = None

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project, location, queue, task):
        return f"{self.queue_path(project, location, queue)}/tasks/{task}"

    def create_task(self, request):
        if self.fail_with is not None:
            raise self.fail_with
        names = [t["task"].get("name") for t in self.tasks if t["task"].get("name")]
        if request["task"].get("name") in names:
            raise AlreadyExists("task exists")
        self.tasks.append(request)
        if self.after_create:
            self.after_create(request)

    def deliver(self, request, client, token="good-token"):
        """What Cloud Tasks does: POST the body to the URL with the OIDC token."""
        http = request["task"]["http_request"]
        path = http["url"].removeprefix(TARGET)
        assert http["url"].startswith(TARGET)
        return client.post(path, content=http["body"], headers={**http["headers"], "Authorization": f"Bearer {token}"})


@pytest.fixture()
def queue(monkeypatch):
    q = FakeQueue()
    s = get_settings()
    for name, value in dict(task_backend="cloudtasks", job_backend="cloudtasks", cloud_tasks_project="agrin-prod",
                            cloud_tasks_location="asia-south1", task_target_url=TARGET, task_service_account=SA,
                            job_payload_bucket="agrin-payloads", cloud_tasks_queue="agrin-tasks",
                            cloud_tasks_job_queue="agrin-jobs").items():
        monkeypatch.setattr(s, name, value)
    monkeypatch.setattr(tasks, "_cloud_client", q)
    store = payload_store.MemoryPayloadStore()
    payload_store.use_payload_store(store)

    def verify(token):
        if token != "good-token":
            raise ValueError("bad token")
        return {"email": SA, "email_verified": True, "aud": TARGET}

    monkeypatch.setattr(auth, "_verify_task_oidc", verify)
    q.payloads = store
    yield q
    payload_store.use_payload_store(None)


# ------------------------------------------------------------------ configuration
def _s(**kw):
    return Settings(_env_file=None, **kw)


def test_cloud_tasks_needs_its_settings_and_local_needs_nothing():
    assert configuration_problems(_s()) == []
    text = " ".join(configuration_problems(_s(task_backend="cloudtasks")))
    assert "TASK_TARGET_URL" in text and "TASK_SERVICE_ACCOUNT" in text
    assert "JOB_PAYLOAD_BUCKET" in " ".join(configuration_problems(
        _s(job_backend="cloudtasks", cloud_tasks_project="p", task_target_url=TARGET, task_service_account=SA)))
    complete = _s(task_backend="cloudtasks", job_backend="cloudtasks", cloud_tasks_project="p",
                  task_target_url=TARGET, task_service_account=SA, job_payload_bucket="b")
    assert configuration_problems(complete) == []
    assert configuration_problems(_s(task_backend="sqs"))  # a typo is caught at start-up, not at 2 a.m.
    with pytest.raises(RuntimeError, match="TASK_TARGET_URL"):
        config.check_production_settings(_s(task_backend="cloudtasks"))


# ------------------------------------------------------------------ the adapter
def test_a_task_becomes_an_authenticated_cloud_tasks_http_request(queue):
    seen = []
    tasks.register("probe-task", lambda p: seen.append(p) or {"ok": True})
    tasks.enqueue("probe-task", {"segment": "IN|Punjab|wheat|hi", "run_date": "2026-09-28"})
    assert seen == []  # nothing ran here: it is queued for whichever instance Cloud Tasks picks
    [request] = queue.tasks
    assert request["parent"] == "projects/agrin-prod/locations/asia-south1/queues/agrin-tasks"
    http = request["task"]["http_request"]
    assert http["http_method"] == "POST" and http["url"] == f"{TARGET}/api/v1/internal/tasks/probe-task"
    assert json.loads(http["body"]) == {"segment": "IN|Punjab|wheat|hi", "run_date": "2026-09-28"}
    assert http["oidc_token"] == {"service_account_email": SA, "audience": TARGET}
    assert "name" not in request["task"]  # notification tasks must stay re-enqueueable when the scheduler retries


def test_a_queue_outage_runs_the_task_locally_instead_of_losing_it(queue):
    ran = threading.Event()
    tasks.register("probe-task", lambda p: ran.set() or {})
    queue.fail_with = RuntimeError("Cloud Tasks unavailable")
    future = tasks.enqueue("probe-task", {})
    future.result(5)
    assert ran.is_set()


def test_dedupe_ids_make_repeat_enqueues_harmless(queue):
    tasks.register("probe-task", lambda p: {})
    tasks.enqueue_cloud("agrin-jobs", "probe-task", {}, dedupe_id="job123")
    tasks.enqueue_cloud("agrin-jobs", "probe-task", {}, dedupe_id="job123")  # AlreadyExists is swallowed
    assert len(queue.tasks) == 1
    assert queue.tasks[0]["task"]["name"].endswith("/queues/agrin-jobs/tasks/job123")


def test_the_real_client_library_is_importable_and_builds_paths():
    from google.cloud import tasks_v2

    assert tasks_v2.CloudTasksClient.queue_path("p", "asia-south1", "q") == "projects/p/locations/asia-south1/queues/q"
    assert tasks_v2.CloudTasksClient.task_path("p", "asia-south1", "q", "t").endswith("/tasks/t")


# ------------------------------------------------------------------ who may run a task
def test_the_task_endpoint_accepts_the_admin_key_or_cloud_tasks_and_nobody_else(queue):
    ran = []
    tasks.register("probe-task", lambda p: ran.append(p) or {"done": True})
    c = TestClient(app)
    url = "/api/v1/internal/tasks/probe-task"
    assert c.post(url, json={}).status_code == 403
    assert c.post(url, json={}, headers={"Authorization": "Bearer forged"}).status_code == 403
    assert c.post(url, json={}, headers={"X-API-Key": "wrong"}).status_code == 403
    assert c.post(url, json={"a": 1}, headers={"X-API-Key": "change-me"}).json() == {"done": True}
    assert c.post(url, json={"a": 2}, headers={"Authorization": "Bearer good-token"}).json() == {"done": True}
    assert ran == [{"a": 1}, {"a": 2}]


def test_a_valid_google_token_for_the_wrong_account_or_unverified_email_is_refused(queue, monkeypatch):
    tasks.register("probe-task", lambda p: {})
    c = TestClient(app)
    url = "/api/v1/internal/tasks/probe-task"
    h = {"Authorization": "Bearer good-token"}
    monkeypatch.setattr(auth, "_verify_task_oidc", lambda t: {"email": "attacker@evil.iam.gserviceaccount.com", "email_verified": True})
    assert c.post(url, json={}, headers=h).status_code == 403
    monkeypatch.setattr(auth, "_verify_task_oidc", lambda t: {"email": SA, "email_verified": False})
    assert c.post(url, json={}, headers=h).status_code == 403
    monkeypatch.setattr(get_settings(), "task_service_account", None)  # OIDC not configured at all
    monkeypatch.setattr(auth, "_verify_task_oidc", lambda t: {"email": SA, "email_verified": True})
    assert c.post(url, json={}, headers=h).status_code == 403


def test_the_other_admin_endpoints_still_refuse_cloud_tasks_tokens(queue):
    """The queue's identity may run tasks, nothing else."""
    c = TestClient(app)
    assert c.post("/api/v1/admin/jobs/purge", headers={"Authorization": "Bearer good-token"}).status_code == 403
    assert c.post("/api/v1/admin/notifications/dispatch", headers={"Authorization": "Bearer good-token"}).status_code == 403


# ------------------------------------------------------------------ AI jobs through the queue
def _photo():
    return {"image": ("leaf.jpg", io.BytesIO(b"\xff\xd8leafbytes"), "image/jpeg")}


def test_a_photo_diagnosis_is_parked_queued_and_finished_by_a_worker(env, queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    seen = []
    monkeypatch.setattr(diagnosis, "diagnose", lambda data, mime, crop, country, notes:
                        seen.append(data) or diagnosis.assemble({"status": "healthy", "confidence": 0.9}))
    r = env.client.post("/api/v1/diagnosis/jobs", files=_photo())
    assert r.status_code == 202 and r.json()["status"] == "queued"
    job_id = r.json()["job_id"]

    [request] = queue.tasks  # the API instance's part is over: a task, and the photo in the bucket
    assert request["parent"].endswith("/queues/agrin-jobs")
    assert json.loads(request["task"]["http_request"]["body"]) == {"job_id": job_id}
    assert b"leafbytes" not in request["task"]["http_request"]["body"]  # the photo is not in the task
    assert request["task"]["name"].endswith(f"/tasks/{job_id}")
    assert queue.payloads.get(job_id) == b"\xff\xd8leafbytes"
    assert env.client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"

    worker = TestClient(app)  # "another instance"
    assert queue.deliver(request, worker).status_code == 200
    done = env.client.get(f"/api/v1/jobs/{job_id}").json()
    assert done["status"] == "done" and done["result"]["status"] == "healthy"
    assert seen == [b"\xff\xd8leafbytes"]
    assert queue.payloads.get(job_id) is None  # the photo is deleted as soon as the worker is done


def test_a_task_delivered_twice_does_the_work_once(env, queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    calls = []
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: calls.append(1) or RESULT)
    pid = _plot(env.client)
    env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    [request] = queue.tasks
    worker = TestClient(app)
    assert queue.deliver(request, worker).status_code == 200
    assert queue.deliver(request, worker).status_code == 200  # Cloud Tasks delivers at least once
    assert len(calls) == 1


def test_a_quick_answer_still_comes_back_in_the_same_response(env, queue, monkeypatch):
    """The API waits briefly on the job row, so a cached answer is as fast as before."""
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 3.0)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    worker = TestClient(app)
    queue.after_create = lambda request: threading.Timer(0.2, queue.deliver, (request, worker)).start()
    pid = _plot(env.client)
    started = time.monotonic()
    r = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert 0.15 < time.monotonic() - started < 2.5


def test_when_the_queue_is_down_the_farmer_is_told_at_once(env, queue):
    queue.fail_with = RuntimeError("Cloud Tasks unavailable")
    r = env.client.post("/api/v1/diagnosis/jobs", files=_photo())
    assert r.status_code == 503 and "busy" in r.json()["detail"]
    assert len(queue.payloads) == 0  # the parked photo was removed again


def test_an_expired_photo_fails_the_job_instead_of_crashing_the_worker(env, queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    monkeypatch.setattr(diagnosis, "diagnose", lambda data, *a: diagnosis.assemble({"status": "healthy", "confidence": 1})
                        if data else (_ for _ in ()).throw(RuntimeError("no photo")))
    r = env.client.post("/api/v1/diagnosis/jobs", files=_photo())
    job_id = r.json()["job_id"]
    queue.payloads.delete(job_id)  # the bucket's lifecycle rule removed it before a worker got there
    assert queue.deliver(queue.tasks[0], TestClient(app)).status_code == 200
    body = env.client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "failed" and body["error"]["status"] == 502


def test_a_double_tap_while_queued_is_the_same_job_and_one_task(env, queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    pid = _plot(env.client)
    a = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    b = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()
    assert a["job_id"] == b["job_id"] and len(queue.tasks) == 1


def test_a_farmer_who_left_still_gets_the_push_from_whichever_instance_runs_the_job(env, queue, monkeypatch):
    from app.services import notifications

    monkeypatch.setattr(get_settings(), "job_fast_wait_s", 0.05)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: RESULT)
    sent = []
    monkeypatch.setattr(notifications, "send_push", lambda token, title, body, data=None: sent.append(token) or True)
    env.client.post("/api/v1/me/fcm-token", json={"token": "tok-1"})
    pid = _plot(env.client)
    job_id = env.client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs").json()["job_id"]
    assert env.client.post(f"/api/v1/jobs/{job_id}/notify").json()["status"] == "will_notify"
    queue.deliver(queue.tasks[0], TestClient(app))
    assert sent == ["tok-1"]
    with env.Session() as db:
        assert db.scalars(select(Job.status)).one() == "done"
