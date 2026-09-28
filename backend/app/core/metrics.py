"""Prometheus metrics: what the service is doing, in numbers an alert can watch.

Exposed at GET /metrics (needs the admin key, as `X-API-Key` or `Authorization: Bearer <key>`, which is how
Prometheus' `authorization` scrape setting sends it). The alert rules in ops/alerts.yml and the dashboard in
docs/OPERATIONS.md are written against these names, and tests/test_observability.py checks they all exist.

Labels are kept low-cardinality on purpose: HTTP metrics use the route *template* (`/plots/{plot_id}/soil`), never
the raw path, so farmer and plot ids never become time series.
"""

from prometheus_client import Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

HTTP_REQUESTS = Counter("agrin_http_requests_total", "HTTP requests handled", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "agrin_http_request_duration_seconds", "Time to answer an HTTP request", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
GEMINI_CALLS = Counter("agrin_gemini_calls_total", "Calls to Gemini by outcome", ["model", "outcome"])
GEMINI_BUSY = Counter("agrin_gemini_busy_total", "Requests told Gemini is busy (no free concurrency slot)")
JOBS = Counter("agrin_jobs_total", "AI jobs by kind and stage (accepted, deduplicated, done, failed)",
               ["kind", "stage"])
RATE_LIMITED = Counter("agrin_rate_limited_total", "Requests refused by a rate limit", ["scope"])

# Requests that would only add noise: probes and the scrape itself.
UNMEASURED_PATHS = ("/metrics", "/api/v1/health", "/api/v1/ready")


class StateCollector(Collector):
    """Reads live state (caches, circuit breakers, queues, DB pool) at scrape time instead of tracking it twice."""

    def collect(self):
        from app.core import cache
        from app.core.config import get_settings
        from app.db import engine
        from app.services import gemini_client, jobs

        entries = GaugeMetricFamily("agrin_cache_entries", "Entries held by each in-process cache", labels=["cache"])
        hits = CounterMetricFamily("agrin_cache_hits", "Cache hits", labels=["cache"])
        misses = CounterMetricFamily("agrin_cache_misses", "Cache misses (the work was done)", labels=["cache"])
        for name, st in cache.stats().items():
            entries.add_metric([name], st["entries"])
            hits.add_metric([name], st["hits"])
            misses.add_metric([name], st["misses"])
        yield from (entries, hits, misses)

        circuit = GaugeMetricFamily("agrin_gemini_circuit_open", "1 while a Gemini model is paused after repeated "
                                    "overload errors", labels=["model"])
        for model, is_open in gemini_client.circuit_states().items():
            circuit.add_metric([model], 1 if is_open else 0)
        yield circuit

        s = get_settings()
        yield GaugeMetricFamily("agrin_job_queue_depth", "AI jobs accepted but not finished on this instance",
                                value=jobs.queue_depth())
        yield GaugeMetricFamily("agrin_job_queue_max", "Most unfinished jobs this instance accepts",
                                value=s.job_queue_max)
        # Always exported, so alerts never silently lose their input; pools that cannot report usage (SQLite's
        # single-connection test pool) show 0 in use.
        in_use = engine.pool.checkedout() if hasattr(engine.pool, "checkedout") else 0
        yield GaugeMetricFamily("agrin_db_pool_checked_out", "Database connections in use", value=in_use)
        yield GaugeMetricFamily("agrin_db_pool_capacity", "Most database connections this instance may open",
                                value=s.db_pool_size + s.db_max_overflow)


_collector_registered = False


def register_collector() -> None:
    global _collector_registered
    if not _collector_registered:
        from prometheus_client import REGISTRY

        REGISTRY.register(StateCollector())
        _collector_registered = True
