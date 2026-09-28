"""Writes grafana/dashboards/agro-nova.json. Edit the PANELS list and re-run: python build_dashboard.py"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}

# (title, unit, [(promql, legend)], kind, description)
PANELS = [
    ("API up", "none", [('up{job="agro-nova-api"}', "up")], "stat",
     "1 while Prometheus can scrape the API. 0 means the API is down or the admin key file is wrong."),
    ("Which model vendor answers (calls per minute)", "short",
     [('sum by (model, outcome) (rate(agrin_gemini_calls_total[5m])) * 60', "{{model}} {{outcome}}")], "timeseries",
     "openai / anthropic / Gemini model names by outcome. success is good; error, http_503 and http_429 are "
     "failures that made the request move on to the next vendor."),
    ("Vendor paused (circuit open)", "none", [("agrin_gemini_circuit_open", "{{model}}")], "timeseries",
     "1 = that vendor failed 3 times in a row and is skipped for 30 s."),
    ("Slowest screens: p95 seconds by route", "s",
     [('histogram_quantile(0.95, sum by (le, route) (rate(agrin_http_request_duration_seconds_bucket[5m])))',
       "{{route}}")], "timeseries", "95% of requests to each route finish faster than this."),
    ("Requests per second by route", "reqps",
     [("sum by (route) (rate(agrin_http_requests_total[5m]))", "{{route}}")], "timeseries", ""),
    ("Errors per second (5xx) by route", "reqps",
     [('sum by (route) (rate(agrin_http_requests_total{status=~"5.."}[5m]))', "{{route}}")], "timeseries", ""),
    ("AI jobs by stage (per minute)", "short",
     [("sum by (kind, stage) (rate(agrin_jobs_total[5m])) * 60", "{{kind}} {{stage}}")], "timeseries",
     "accepted, deduplicated, done, failed. Many failed means the model vendors are struggling."),
    ("Jobs waiting in the queue", "short",
     [("agrin_job_queue_depth", "waiting"), ("agrin_job_queue_max", "limit")], "timeseries", ""),
    ("Cache hit ratio", "percentunit",
     [("sum by (cache) (rate(agrin_cache_hits_total[10m])) / "
       "clamp_min(sum by (cache) (rate(agrin_cache_hits_total[10m]) + rate(agrin_cache_misses_total[10m])), 1e-9)",
       "{{cache}}")], "timeseries", "High is good: the answer was reused instead of recomputed."),
    ("Requests refused by rate limits (per minute)", "short",
     [("sum by (scope) (rate(agrin_rate_limited_total[5m])) * 60", "{{scope}}")], "timeseries", ""),
    ("Database connections in use", "short",
     [("agrin_db_pool_checked_out", "in use"), ("agrin_db_pool_capacity", "capacity")], "timeseries", ""),
]


def panel(i, title, unit, queries, kind, desc):
    stat = kind == "stat"
    return {
        "id": i + 1, "type": kind, "title": title, "description": desc, "datasource": DS,
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"datasource": DS, "expr": e, "legendFormat": leg, "refId": chr(65 + n)}
                    for n, (e, leg) in enumerate(queries)],
        "options": ({"reduceOptions": {"calcs": ["lastNotNull"]}} if stat
                    else {"legend": {"displayMode": "list", "placement": "bottom"}}),
    }


panels = [panel(i, *p) for i, p in enumerate(PANELS)]
y = 4  # the "API up" stat is a full-width strip on top; the rest are 12 x 8 tiles in two columns
panels[0]["gridPos"].update(x=0, y=0, w=24, h=4)
for n, p in enumerate(panels[1:]):
    p["gridPos"].update(x=12 * (n % 2), y=y + 8 * (n // 2), w=12, h=8)

dash = {"uid": "agro-nova", "title": "Agro Nova API", "tags": ["agro-nova"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "refresh": "15s", "time": {"from": "now-1h", "to": "now"},
        "panels": panels}
out = Path(__file__).parent / "grafana" / "dashboards" / "agro-nova.json"
out.write_text(json.dumps(dash, indent=1), encoding="utf-8")
print("wrote", out, len(panels), "panels")
