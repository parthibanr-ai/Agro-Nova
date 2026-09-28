"""Push notifications: one digest per audience, sent in batches, safe to retry, no per-farmer work."""

import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.db import get_db, migrate
from app.main import app
from app.models import NotificationRun, Plot, User
from app.services import notifications, tasks
from app.services.notifications import SendOutcome, Segment

ADMIN = {"X-API-Key": "change-me"}
TODAY = date.today()


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
        engine = create_engine(f"sqlite:///{tmp_path / 'n.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    migrate(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def db_dep():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_dep
    monkeypatch.setattr(notifications, "SessionLocal", Session)
    monkeypatch.setattr(tasks, "inline", True)  # run the queued tasks immediately so tests are deterministic
    monkeypatch.setattr(notifications, "get_enso_state", lambda: _enso())
    sent: list[dict] = []

    def fake_send(tokens, title, body, data=None):
        sent.append({"tokens": list(tokens), "title": title, "topic": (data or {}).get("topic")})
        return SendOutcome(sent=len(tokens))

    monkeypatch.setattr(notifications, "send_multicast", fake_send)
    yield type("Env", (), {"client": TestClient(app), "Session": Session, "engine": engine, "sent": sent})
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _enso():
    from app.services.enso import EnsoState

    return EnsoState(True, "el_nino", "strong", 1.8, "JJA 2026", "steady")


def add_farmers(Session, n, *, state="Punjab", crop="wheat", language="hi", prefix="f", with_token=True, with_plot=True):
    with Session() as db:
        for i in range(n):
            uid = f"{prefix}-{i:05d}"
            db.add(User(uid=uid, language=language, country="IN", state=state, primary_crop=crop if with_plot else None,
                        fcm_token=f"tok-{uid}" if with_token else None, livestock={}))
            if with_plot:
                db.add(Plot(owner_uid=uid, name="P", country="IN", state=state, crop=crop, corners=[], area_m2=10000,
                            centroid_lat=30.9, centroid_lon=75.85))
        db.commit()


# ------------------------------------------------------------------ audiences
def test_farmers_are_grouped_into_audiences_and_counted(env):
    add_farmers(env.Session, 5, state="Punjab", crop="wheat", language="hi", prefix="a")
    add_farmers(env.Session, 3, state="Punjab", crop="wheat", language="en", prefix="b")
    add_farmers(env.Session, 2, state="Haryana", crop="rice", language="hi", prefix="c")
    add_farmers(env.Session, 4, state="Punjab", crop=None, language="hi", prefix="d", with_plot=False)
    add_farmers(env.Session, 6, state="Punjab", crop="wheat", language="hi", prefix="e", with_token=False)  # no device
    with env.Session() as db:
        found = dict(notifications.list_segments(db))
    assert found == {
        Segment("IN", "Punjab", "wheat", "hi"): 5,
        Segment("IN", "Punjab", "wheat", "en"): 3,
        Segment("IN", "Haryana", "rice", "hi"): 2,
        Segment("IN", "Punjab", None, "hi"): 4,
    }


def test_a_farmer_with_several_plots_is_counted_once_under_their_first_crop(env):
    # Go through the API so this covers how the app maintains the farmer's first crop.
    corners = [{"lat": 30.9, "lon": 75.85}, {"lat": 30.9, "lon": 75.851}, {"lat": 30.901, "lon": 75.851},
               {"lat": 30.901, "lon": 75.85}]
    h = {"X-Dev-User": "multi"}

    def plot(crop):
        r = env.client.post("/api/v1/plots", json={"name": crop, "crop": crop, "country": "IN", "state": "Punjab",
                                                    "corners": corners}, headers=h)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    env.client.post("/api/v1/me/fcm-token", json={"token": "tok-multi"}, headers=h)
    first, second = plot("wheat"), plot("rice")
    with env.Session() as db:
        assert dict(notifications.list_segments(db)) == {Segment("IN", "Punjab", "wheat", "en"): 1}
    # deleting the first plot hands the audience over to the next plot's crop
    assert env.client.delete(f"/api/v1/plots/{first}", headers=h).status_code == 204
    with env.Session() as db:
        assert dict(notifications.list_segments(db)) == {Segment("IN", "Punjab", "rice", "en"): 1}
    assert env.client.delete(f"/api/v1/plots/{second}", headers=h).status_code == 204
    with env.Session() as db:
        assert dict(notifications.list_segments(db)) == {Segment("IN", "Punjab", None, "en"): 1}


def test_segment_keys_round_trip():
    for seg in (Segment("IN", "Punjab", "wheat", "hi"), Segment("IN", None, None, "en")):
        assert Segment.from_key(seg.key) == seg


# ------------------------------------------------------------------ dispatch
def test_dry_run_reports_audiences_without_sending_or_exposing_farmers(env):
    add_farmers(env.Session, 20, prefix="dry")
    assert env.client.post("/api/v1/admin/notifications/dispatch").status_code == 403
    r = env.client.post("/api/v1/admin/notifications/dispatch", headers=ADMIN).json()
    assert r["dry_run"] is True and r["segments"] == 1 and r["users"] == 20
    digest = r["digests"][0]
    assert {m["topic"] for m in digest["messages"]} >= {"scheme", "weather", "market"} and digest["sent"] == 0
    assert "tok-" not in str(r) and "dry-0" not in str(r), "no farmer ids or device tokens in the report"
    assert env.sent == []


def test_the_digest_is_built_once_per_audience_not_once_per_farmer(env, monkeypatch):
    add_farmers(env.Session, 300, prefix="a")
    add_farmers(env.Session, 200, language="en", prefix="b")
    built = []
    real = notifications.build_digest
    monkeypatch.setattr(notifications, "build_digest", lambda *a, **k: built.append(a) or real(*a, **k))
    r = env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN).json()
    assert r["queued"] == 2 and r["users"] == 500
    assert len(built) == 2, f"500 farmers, 2 audiences: expected 2 digests, built {len(built)}"


def test_messages_go_out_in_batches_of_500_and_every_device_gets_each_message_once(env):
    add_farmers(env.Session, 1200, prefix="big")
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    assert env.sent, "nothing was sent"
    assert max(len(c["tokens"]) for c in env.sent) <= 500
    by_message: dict = {}
    for c in env.sent:
        by_message.setdefault((c["topic"], c["title"]), []).extend(c["tokens"])
    assert {t for t, _ in by_message} >= {"scheme", "weather", "market"}
    for message, tokens in by_message.items():
        assert len(tokens) == 1200 and len(set(tokens)) == 1200, f"{message}: every device exactly once"
    calls_per_message = len([c for c in env.sent if c["topic"] == "weather"])
    assert calls_per_message == 3  # 1200 devices / 500 per call = 3 FCM calls, not 1200


def test_the_database_work_does_not_grow_with_the_number_of_farmers(env):
    add_farmers(env.Session, 1000, prefix="q")
    statements = []
    event.listen(env.engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    # 1000 farmers = 2 pages: a handful of statements for planning, claiming, paging and finishing. The old job ran
    # at least one plot query per farmer (1000+).
    assert len(statements) < 25, f"{len(statements)} statements for 1000 farmers"


def test_running_the_same_day_twice_sends_nothing_twice(env):
    add_farmers(env.Session, 50, prefix="twice")
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    first = len(env.sent)
    again = env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN).json()
    assert again["queued"] == 1 and len(env.sent) == first, "a duplicate dispatch must not resend"
    # ...but tomorrow is a new day
    env.client.post(f"/api/v1/admin/notifications/dispatch?dry_run=false&day={TODAY + timedelta(days=1)}", headers=ADMIN)
    assert len(env.sent) == 2 * first


def test_a_crashed_audience_is_retried_and_finished(env, monkeypatch):
    add_farmers(env.Session, 30, prefix="crash")
    real = notifications.send_multicast
    state = {"boom": True}

    def flaky(tokens, title, body, data=None):
        if state["boom"]:
            raise RuntimeError("FCM unreachable")
        return real(tokens, title, body, data)

    monkeypatch.setattr(notifications, "send_multicast", flaky)
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    assert env.client.get("/api/v1/admin/notifications/runs", headers=ADMIN).json()["segments"] == {"failed": 1}
    state["boom"] = False
    monkeypatch.setattr(notifications, "send_multicast", lambda t, ti, b, d=None: SendOutcome(sent=len(t)))
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)  # the scheduler retries
    summary = env.client.get("/api/v1/admin/notifications/runs", headers=ADMIN).json()
    assert summary["segments"] == {"done": 1} and summary["users"] == 30


def test_a_run_abandoned_mid_way_is_taken_over_after_the_stale_period(env):
    add_farmers(env.Session, 10, prefix="stale")
    seg = notifications.list_segments(env.Session())[0][0]
    with env.Session() as db:  # an instance claimed it half an hour ago and died
        db.add(NotificationRun(run_date=TODAY, segment_key=seg.key, status="running",
                               started_at=notifications._now() - timedelta(seconds=notifications.STALE_RUN_S + 60)))
        db.commit()
    out = notifications.run_segment({"run_date": TODAY.isoformat(), "segment": seg.key})
    assert out["status"] == "done" and out["users"] == 10
    fresh = notifications.run_segment({"run_date": TODAY.isoformat(), "segment": seg.key})
    assert fresh["skipped"] is True  # now finished: not run again


def test_a_run_that_is_still_in_progress_elsewhere_is_left_alone(env):
    add_farmers(env.Session, 10, prefix="busy")
    seg = notifications.list_segments(env.Session())[0][0]
    with env.Session() as db:
        db.add(NotificationRun(run_date=TODAY, segment_key=seg.key, status="running", started_at=notifications._now()))
        db.commit()
    assert notifications.run_segment({"run_date": TODAY.isoformat(), "segment": seg.key})["skipped"] is True
    assert env.sent == []


# ------------------------------------------------------------------ dead device tokens
def test_dead_device_tokens_are_cleared_so_tomorrow_skips_them(env, monkeypatch):
    add_farmers(env.Session, 6, prefix="dead")
    dead = {"tok-dead-00001", "tok-dead-00004"}
    monkeypatch.setattr(notifications, "send_multicast",
                        lambda t, ti, b, d=None: SendOutcome(sent=len(t) - len(dead & set(t)), failed=len(dead & set(t)),
                                                             dead_tokens=sorted(dead & set(t))))
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    with env.Session() as db:
        tokens = {u.uid: u.fcm_token for u in db.scalars(select(User))}
    assert tokens["dead-00001"] is None and tokens["dead-00004"] is None
    assert tokens["dead-00000"] == "tok-dead-00000"
    summary = env.client.get("/api/v1/admin/notifications/runs", headers=ADMIN).json()
    assert summary["dead_tokens_cleared"] == 2 and summary["failed"] > 0
    with env.Session() as db:
        assert dict(notifications.list_segments(db)) == {Segment("IN", "Punjab", "wheat", "hi"): 4}


def test_multicast_reports_dead_tokens_from_firebase(monkeypatch):
    class Unregistered(Exception):
        pass

    class Mismatch(Exception):
        pass

    class Resp:
        def __init__(self, ok, exc=None):
            self.success, self.exception = ok, exc

    class FakeMessaging:
        UnregisteredError, SenderIdMismatchError = Unregistered, Mismatch
        Notification = lambda **kw: kw  # noqa: E731
        MulticastMessage = lambda **kw: kw  # noqa: E731

        @staticmethod
        def send_each_for_multicast(msg):
            return type("B", (), {"responses": [Resp(True), Resp(False, Unregistered()), Resp(False, RuntimeError("x"))]})()

    monkeypatch.setattr(notifications, "_firebase_messaging", lambda: FakeMessaging)
    out = notifications.send_multicast(["a", "b", "c"], "t", "b")
    assert (out.sent, out.failed, out.dead_tokens) == (1, 2, ["b"])  # only "unregistered" tokens are dead


def test_without_firebase_credentials_nothing_is_sent_and_nothing_breaks(monkeypatch):
    def no_creds():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(notifications, "_firebase_messaging", no_creds)
    assert notifications.send_multicast(["a"], "t", "b") == SendOutcome()
    assert notifications.send_push("a", "t", "b") is False


# ------------------------------------------------------------------ queue endpoints
def test_the_http_task_target_runs_the_same_handler(env):
    add_farmers(env.Session, 5, prefix="http")
    seg = notifications.list_segments(env.Session())[0][0]
    body = {"run_date": TODAY.isoformat(), "segment": seg.key}
    assert env.client.post("/api/v1/internal/tasks/notify-segment", json=body).status_code == 403
    r = env.client.post("/api/v1/internal/tasks/notify-segment", json=body, headers=ADMIN)
    assert r.status_code == 200 and r.json()["users"] == 5
    assert env.client.post("/api/v1/internal/tasks/nope", json={}, headers=ADMIN).status_code == 404


def test_progress_endpoint_summarises_a_days_run(env):
    add_farmers(env.Session, 40, prefix="p1", language="hi")
    add_farmers(env.Session, 10, prefix="p2", language="en")
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    s = env.client.get("/api/v1/admin/notifications/runs", headers=ADMIN).json()
    assert s["segments"] == {"done": 2} and s["users"] == 50 and s["sent"] > 0
    assert env.client.get("/api/v1/admin/notifications/runs", headers=ADMIN, params={"day": "2020-01-01"}).json()["users"] == 0


# ------------------------------------------------------------------ a farmer is not spammed
def _titles(env):
    return sorted({c["title"] for c in env.sent if c["topic"] == "scheme"})


def test_at_most_two_new_schemes_go_out_per_day(env):
    add_farmers(env.Session, 10, prefix="cap")
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    assert 1 <= len(_titles(env)) <= 2


def test_a_scheme_is_announced_once_and_the_backlog_is_worked_through_over_days(env):
    from app.services import advice

    add_farmers(env.Session, 10, prefix="backlog")
    matching = {s["name"] for s in advice.matching_schemes("IN", "Punjab", "wheat", None)}
    assert len(matching) > 4, "this test needs a backlog of schemes"
    seen_titles: list[str] = []
    for day in range(len(matching)):  # more than enough days to exhaust the backlog
        before = len(env.sent)
        env.client.post(f"/api/v1/admin/notifications/dispatch?dry_run=false&day={TODAY + timedelta(days=day)}",
                        headers=ADMIN)
        today_titles = sorted({c["title"] for c in env.sent[before:] if c["topic"] == "scheme"})
        assert set(today_titles).isdisjoint(seen_titles), f"day {day}: a scheme was repeated: {today_titles}"
        seen_titles += today_titles
    assert {t.removeprefix("Scheme: ") for t in seen_titles} == matching, "every scheme is eventually announced"
    # once the backlog is empty, the daily weather and market messages still go out
    last = len(env.sent)
    env.client.post(f"/api/v1/admin/notifications/dispatch?dry_run=false&day={TODAY + timedelta(days=60)}",
                    headers=ADMIN)
    assert {c["topic"] for c in env.sent[last:]} == {"weather", "market"}


def test_a_failed_run_does_not_use_up_its_schemes(env, monkeypatch):
    add_farmers(env.Session, 10, prefix="retry")
    monkeypatch.setattr(notifications, "send_multicast", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    monkeypatch.setattr(
        notifications, "send_multicast",
        lambda t, ti, b, d=None: env.sent.append({"tokens": t, "title": ti, "topic": (d or {}).get("topic")})
        or SendOutcome(sent=len(t)))
    env.client.post("/api/v1/admin/notifications/dispatch?dry_run=false", headers=ADMIN)
    assert 1 <= len(_titles(env)) <= 2, "the retry announces the schemes the failed attempt never delivered"
