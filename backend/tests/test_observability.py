"""Logs, request ids, metrics, probes and the alert rules that depend on them."""

import io
import json
import logging
import re
from pathlib import Path

import pytest
import yaml
from prometheus_client import generate_latest

from app.core import logging_config, metrics
from tests.conftest import NASHIK

ADMIN = {"X-API-Key": "change-me"}
ALERTS = Path(__file__).resolve().parents[2] / "ops" / "alerts.yml"


def _scrape(client) -> str:
    r = client.get("/metrics", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.text


def _plot(client):
    return client.post("/api/v1/plots", json={"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab",
                                              "corners": NASHIK}).json()["id"]


# ------------------------------------------------------------------ /metrics access
def test_metrics_need_the_admin_key_in_either_form(client):
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"X-API-Key": "wrong"}).status_code == 403
    assert client.get("/metrics", headers=ADMIN).status_code == 200
    # what Prometheus' `authorization:` scrape setting sends
    assert client.get("/metrics", headers={"Authorization": "Bearer change-me"}).status_code == 200
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 403


def test_metrics_can_be_switched_off(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "metrics_enabled", False)
    assert client.get("/metrics", headers=ADMIN).status_code == 404


# ------------------------------------------------------------------ HTTP metrics
def test_requests_are_counted_by_route_template_never_by_raw_path(client):
    pid = _plot(client)
    client.get(f"/api/v1/plots/{pid}/soil")
    client.get(f"/api/v1/plots/{pid}/soil")
    client.get("/api/v1/plots/no-such-plot/soil")  # a 404 for a different id
    text = _scrape(client)
    assert pid not in text and "no-such-plot" not in text, "plot ids must never become metric labels"
    m = re.search(r'agrin_http_requests_total\{method="GET",route="/api/v1/plots/\{plot_id\}/soil",status="200"\} (\S+)', text)
    assert m and float(m.group(1)) >= 2
    assert 'route="/api/v1/plots/{plot_id}/soil",status="404"' in text
    assert "agrin_http_request_duration_seconds_bucket" in text


def test_unknown_paths_share_one_label_so_scanners_cannot_flood_the_metrics(client):
    for i in range(20):
        client.get(f"/wp-admin/{i}.php")
    text = _scrape(client)
    assert 'route="unmatched"' in text and "wp-admin" not in text


def test_probes_and_the_scrape_itself_are_not_measured(client):
    client.get("/api/v1/health")
    client.get("/api/v1/ready")
    text = _scrape(client)
    assert "/api/v1/health" not in text and "/api/v1/ready" not in text and 'route="/metrics"' not in text


# ------------------------------------------------------------------ request ids and logs
def test_every_response_carries_a_request_id_and_honours_the_callers(client):
    generated = client.get("/api/v1/meta/countries").headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{16}", generated)
    assert client.get("/api/v1/meta/countries", headers={"X-Request-ID": "trace-abc-123"}).headers["X-Request-ID"] == "trace-abc-123"
    long = client.get("/api/v1/meta/countries", headers={"X-Request-ID": "x" * 500}).headers["X-Request-ID"]
    assert len(long) == 64


def test_log_lines_written_while_handling_a_request_carry_its_id(client, monkeypatch):
    from app.services import personalized_advice

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(logging_config.RequestIdFilter())
    handler.setFormatter(logging.Formatter("%(request_id)s|%(message)s"))
    logging.getLogger().addHandler(handler)
    monkeypatch.setattr(personalized_advice, "call_gemini",
                        lambda prompt: (_ for _ in ()).throw(RuntimeError("503 overloaded")))
    try:
        pid = _plot(client)
        # a sync route: it runs in a worker thread, and the id must survive the hop
        client.get(f"/api/v1/resilience?plot_id={pid}", headers={"X-Request-ID": "req-42"})
        client.get(f"/api/v1/resilience?plot_id={pid}", headers={"X-Request-ID": "req-43"})
    finally:
        logging.getLogger().removeHandler(handler)
    lines = [ln for ln in stream.getvalue().splitlines() if "advice failed" in ln]
    assert lines, "the route should have logged the failed advice"
    assert lines[0].startswith("req-42|") and lines[1].startswith("req-43|"), lines


def test_json_log_format_is_one_parsable_object_per_line():
    rec = logging.LogRecord("agrin.test", logging.WARNING, __file__, 1, "cache %s", ("full",), None)
    rec.request_id = "abc"
    out = json.loads(logging_config.JsonFormatter().format(rec))
    assert out["severity"] == "WARNING" and out["message"] == "cache full" and out["request_id"] == "abc"
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        rec2 = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert "ValueError: bad" in json.loads(logging_config.JsonFormatter().format(rec2))["exception"]


# ------------------------------------------------------------------ probes
def test_health_is_a_cheap_liveness_check_and_ready_checks_the_database(client, monkeypatch):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    r = client.get("/api/v1/ready")
    assert r.status_code == 200 and r.json()["database"] == "ok" and "job_queue_depth" in r.json()

    class Broken:
        def __enter__(self):
            raise RuntimeError("database is down")

        def __exit__(self, *a):
            pass

    from app.api import routes

    monkeypatch.setattr(routes, "SessionLocal", Broken)
    assert client.get("/api/v1/ready").status_code == 503
    assert client.get("/api/v1/health").status_code == 200, "liveness must not depend on the database"


# ------------------------------------------------------------------ domain metrics
def test_state_metrics_report_caches_queues_and_pool(client):
    from app.core import cache

    cache.TTLCache("probe-cache").get_or_compute("k", lambda: 1)
    text = _scrape(client)
    for name in ("agrin_cache_entries", "agrin_cache_hits_total", "agrin_cache_misses_total", "agrin_job_queue_depth",
                 "agrin_job_queue_max", "agrin_gemini_circuit_open"):
        assert name in text, name
    assert 'agrin_cache_misses_total{cache="probe-cache"} 1.0' in text


def test_rate_limit_refusals_are_counted(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_rate_per_minute", 1)
    client.get("/api/v1/resilience")
    assert client.get("/api/v1/resilience").status_code == 429
    assert re.search(r'agrin_rate_limited_total\{scope="ai-min"\} [1-9]', _scrape(client))


def test_gemini_outcomes_and_circuit_state_are_counted(monkeypatch):
    from google import genai
    from google.genai import errors

    from app.core.config import get_settings
    from app.services import gemini_client

    gemini_client.reset_state()
    monkeypatch.setattr(get_settings(), "gemini_api_key", "k")
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: None)
    state = {"fail": True}

    class Models:
        def generate_content(self, model, contents, config):
            if state["fail"]:
                raise errors.APIError(503, {"error": {"message": "busy", "status": "UNAVAILABLE"}})
            return type("R", (), {"text": "{}"})()

    class Client:
        def __init__(self, **kw):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    with pytest.raises(Exception):
        gemini_client.generate_json(["x"], 0.1)
    assert any(gemini_client.circuit_states().values()), "three 503s in a row open the circuit"
    text = generate_latest().decode()
    assert re.search(r'agrin_gemini_calls_total\{model="[^"]+",outcome="http_503"\} [1-9]', text)
    assert re.search(r'agrin_gemini_circuit_open\{model="[^"]+"\} 1.0', text)
    gemini_client.reset_state()


def test_job_stages_are_counted(client, monkeypatch):
    from app.services import crop_recommendation, jobs

    jobs.shutdown()
    # (uses the shared in-memory database, so wait for the worker before the next request)
    monkeypatch.setattr(crop_recommendation, "call_gemini", lambda p: {"target_season": {}, "recommendations": []})
    pid = _plot(client)
    r = client.post(f"/api/v1/plots/{pid}/crop-recommendation/jobs")
    assert r.status_code in (200, 202)
    jobs.shutdown()
    text = generate_latest().decode()
    assert 'agrin_jobs_total{kind="crop_recommendation",stage="accepted"}' in text


# ------------------------------------------------------------------ alert rules
def _rules():
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g["rules"]]


def test_alert_file_is_well_formed():
    rules = _rules()
    assert len(rules) >= 8
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names)), "alert names must be unique"
    for r in rules:
        assert r["expr"].strip() and r["for"] and r["labels"]["severity"] in ("page", "ticket", "info"), r["alert"]
        assert r["annotations"]["summary"] and r["annotations"]["runbook"].startswith("docs/OPERATIONS.md#"), r["alert"]


def test_every_metric_an_alert_uses_really_exists(client):
    exposed = set(re.findall(r"^# TYPE (\S+) ", _scrape(client), flags=re.M))
    exposed |= {n + s for n in list(exposed) for s in ("_bucket", "_count", "_sum")}
    exposed |= {n.removesuffix("_total") for n in exposed}  # a counter's family name omits _total
    used = set()
    for r in _rules():
        used |= set(re.findall(r"\bagrin_[a-z_]+", r["expr"]))
    assert used, "no agrin_* metrics found in the alerts"
    missing = {u for u in used if u not in exposed}
    assert not missing, f"alerts reference metrics that the service does not export: {missing}"


def test_every_runbook_section_an_alert_points_to_exists():
    ops = (ALERTS.parent.parent / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    anchors = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
               for h in re.findall(r"^#+\s+(.+)$", ops, flags=re.M)}
    for r in _rules():
        anchor = r["annotations"]["runbook"].split("#", 1)[1]
        assert anchor in anchors, f"{r['alert']}: docs/OPERATIONS.md has no section '#{anchor}'"
