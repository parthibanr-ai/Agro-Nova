# Agro Nova operations guide

For whoever runs the API in production: what to watch, what the alerts mean, and what to do about them. Settings
mentioned here are explained in [env_reference.md](env_reference.md). The alert rules are in
[../ops/alerts.yml](../ops/alerts.yml).

## 1. Connecting monitoring

**Metrics.** `GET /metrics` returns Prometheus metrics. It needs the admin key, sent either as `X-API-Key: <key>` or
as `Authorization: Bearer <key>`. A Prometheus scrape job:

```yaml
scrape_configs:
  - job_name: agro-nova-api          # the alert rules expect this name
    metrics_path: /metrics
    authorization:
      credentials: <ADMIN_API_KEY>   # keep it in a secret, not in the file
    static_configs:
      - targets: ["api.example.org:443"]
    scheme: https
```

On Google Cloud, Managed Service for Prometheus can scrape the same endpoint, and the rules import as they are.
Set `METRICS_ENABLED=false` to turn the endpoint off. Metrics carry route templates (`/plots/{plot_id}/soil`), never
farmer or plot ids.

**Probes.** `GET /api/v1/health` is a liveness check: cheap, never touches the database, use it to decide whether to
restart an instance. `GET /api/v1/ready` is a readiness check: `200` only if the database answers, `503` otherwise,
use it to decide whether to send traffic to an instance.

**Logs.** Set `LOG_FORMAT=json` for one JSON object per line (`severity`, `message`, `request_id`, ...), which Cloud
Logging and most platforms parse without configuration. Every request has an id: the caller's `X-Request-ID` if it
sent one, else a generated one, always returned in the `X-Request-ID` response header and stamped on every log
line written while handling it. When a farmer reports a problem, get that header (or the id from the app's error
report) and search the logs for it.

## 2. What is measured

| Metric | Meaning |
|---|---|
| `agrin_http_requests_total{method,route,status}` | Requests handled |
| `agrin_http_request_duration_seconds` (histogram) | Time to answer, by route |
| `agrin_gemini_calls_total{model,outcome}` | Gemini calls: `success`, `http_503`, `http_429`, `error`, `skipped_circuit_open` |
| `agrin_gemini_circuit_open{model}` | `1` while a model is paused after repeated overload errors |
| `agrin_gemini_busy_total` | Requests told Gemini is busy (no free concurrency slot) |
| `agrin_jobs_total{kind,stage}` | AI jobs: `accepted`, `deduplicated`, `done`, `failed` |
| `agrin_job_queue_depth`, `agrin_job_queue_max` | Unfinished AI jobs on this instance, and the most it accepts |
| `agrin_cache_hits_total`, `agrin_cache_misses_total`, `agrin_cache_entries` | Per cache (`ai-advice`, `weather-cell`, `soilgrids-cell`, ...) |
| `agrin_db_pool_checked_out`, `agrin_db_pool_capacity` | Database connections in use, and the most this instance may open |
| `agrin_rate_limited_total{scope}` | Requests refused by a rate limit |

## 3. Service level objectives

These are proposed targets; agree them with the programme owner before treating them as commitments. Measure over
30 days.

| Objective | Target | How to measure |
|---|---|---|
| Availability | 99.5% of requests are not 5xx | `1 - sum(rate(agrin_http_requests_total{status=~"5.."}[30d])) / sum(rate(agrin_http_requests_total[30d]))` |
| Everyday screens are fast | p95 under 500 ms for schemes, soil, forecast, plot list | `histogram_quantile(0.95, sum by (le) (rate(agrin_http_request_duration_seconds_bucket{route=~"/api/v1/(schemes\|plots\|plots/\\{plot_id\\}/soil\|plots/\\{plot_id\\}/forecast)"}[30d])))` |
| AI answers arrive | 95% of AI jobs finish successfully | `sum(rate(agrin_jobs_total{stage="done"}[30d])) / sum(rate(agrin_jobs_total{stage=~"done\|failed"}[30d]))` |
| Farmers are not turned away | under 1% of requests answered 429 or 503 | `sum(rate(agrin_http_requests_total{status=~"429\|503"}[30d])) / sum(rate(agrin_http_requests_total[30d]))` |

## 4. Dashboard

One page, top to bottom:

1. **Traffic and errors:** `sum(rate(agrin_http_requests_total[5m])) by (status)`.
2. **Latency:** p50/p95/p99 from `agrin_http_request_duration_seconds_bucket`, everyday screens and AI start endpoints separately.
3. **AI health:** `sum(rate(agrin_gemini_calls_total[5m])) by (outcome)`, `agrin_gemini_circuit_open`, `sum(rate(agrin_jobs_total[5m])) by (stage)`.
4. **Queues and connections:** `agrin_job_queue_depth / agrin_job_queue_max`, `agrin_db_pool_checked_out / agrin_db_pool_capacity`, per instance.
5. **Cache effectiveness:** hit ratio per cache, `hits / (hits + misses)`. The `ai-advice` ratio is the main lever on Gemini cost.
6. **Abuse:** `sum(rate(agrin_rate_limited_total[5m])) by (scope)`.

## 5. Alerts and runbooks

Severity: **page** (wake someone), **ticket** (fix in working hours), **info** (watch).

### Instance down
*Alert: AgroNovaDown.* Prometheus cannot scrape an instance. Check the platform (Cloud Run revision status, container restarts and out-of-memory kills). If `/api/v1/ready` answers 503, the database is the cause, see "Database connections exhausted" and the database's own status. If the container crash-loops right after a deploy, roll back to the previous revision.

### High error rate
*Alert: AgroNovaHighErrorRate.* More than 2% of requests are 5xx. In the metrics, find the route and status: `sum by (route, status) (rate(agrin_http_requests_total{status=~"5.."}[5m]))`. `502` and `503` on the AI routes usually mean Gemini, see "Gemini overloaded". Anything else: take a failing request's `X-Request-ID` and search the logs for the stack trace.

### Slow everyday screens
*Alert: AgroNovaReadScreensSlow.* These screens should answer from cache or the database in milliseconds. Slow usually means a cache miss storm or a slow upstream (Open-Meteo, SoilGrids, Earth Engine). Check `agrin_cache_misses_total` by cache and the logs for upstream timeouts. If the database is slow, check its CPU and connection count. A restart empties the caches, so a wave of slow requests right after a deploy is normal and clears within minutes.

### Gemini overloaded
*Alerts: AgroNovaGeminiCircuitOpen, AgroNovaGeminiFailing, AgroNovaGeminiBusy.* Google's model is returning 503/429 (overload or quota), or every concurrency slot is taken. The app degrades gracefully: personalised sections drop out, recommendations and diagnosis answer "try again", and the circuit breaker stops the API from hammering a struggling model. What to do: check Google's status and your project's Gemini quota; raise the quota or switch `GEMINI_USE_VERTEX=true` for higher limits; if `AgroNovaGeminiBusy` fires while Gemini is healthy, raise `GEMINI_MAX_CONCURRENCY` (keep instances x value inside the quota) or add instances.

### AI job queue full
*Alert: AgroNovaJobQueueNearlyFull.* An instance has more than 80% of `JOB_QUEUE_MAX` unfinished AI jobs; at 100% it answers 503 with `Retry-After`. Usually Gemini is slow (see above) or traffic spiked. Add instances, or raise `JOB_WORKERS` if Gemini is healthy and the instance has spare CPU. Do not raise `JOB_QUEUE_MAX` alone: it only lengthens the wait.

### AI jobs failing
*Alert: AgroNovaJobsFailing.* Over a quarter of jobs end in `failed`. The job's stored error says why (`GET /api/v1/jobs/{id}` as the owner, or the logs with the job's request id). Typical causes: Gemini errors (above), Earth Engine or weather upstream errors, a missing `GEMINI_API_KEY`. Jobs lost because an instance died are marked failed after `JOB_LEASE_S`; a burst of those points to instances being killed mid-work (out of memory, or scale-in during a deploy).

### Gemini spend is high
*Alert: AgroNovaAdviceCacheColdSpend.* Fewer than half of AI advice requests are served from cache. Expected right after a restart or a new region going live. If it persists, check that the app version is not sending unusual inputs, that `CACHE_ENABLED` is not `false`, and that `AI_CACHE_TTL_S` is not tiny. With several instances, each keeps its own cache, so more instances lower the ratio; a shared cache (Redis) is the fix at that scale.

### Database connections exhausted
*Alert: AgroNovaDbPoolSaturated.* An instance uses over 90% of its pool; at 100% requests wait `DB_POOL_TIMEOUT_S` and then fail. Find what holds connections (slow queries, long transactions) before raising `DB_POOL_SIZE`. Remember the database has a global limit: instances x (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) must stay below it, otherwise put PgBouncer in front and set `DB_PGBOUNCER=true`.

### Rate limit spike
*Alert: AgroNovaRateLimitSpike.* Many requests are being refused with 429. Look at the `scope` label: `ai-min` or `diagnosis-min` from a few farmers means a stuck or aggressive client (an app version retrying in a loop); `ip-min` means one address is flooding the API. Blocking an address is done at the load balancer or with Cloud Armor, not in the app. If honest farmers are hitting the limits, raise `AI_RATE_PER_MINUTE` or `IP_RATE_PER_MINUTE` (carrier-grade NAT puts many farmers behind one address).

## 6. Load testing

Two tools, for two questions.

**"Did a change make us call upstreams more?"** runs in CI on every change:

```powershell
cd backend
.venv\Scripts\python scripts\simulate_sessions.py 150 0.3 --check
```

It runs 150 farmers through every screen twice against the real application with fake upstreams, and fails if Gemini, Earth Engine or weather calls per farmer rise above the recorded limits. It measures how many upstream calls the code makes, not speed or capacity.

**"Can a deployment carry N farmers?"** uses [k6](https://k6.io) against a staging environment (never production, and never with real keys unless you intend to spend the quota):

```
k6 run -e BASE_URL=https://staging.example.org -e ADMIN_KEY=... loadtest/k6_farmers.js
```

It ramps virtual farmers through a realistic mix of screens and AI jobs, and fails if p95 latency, the error rate or the AI job success rate cross the SLO thresholds above. Pair it with the dashboard in section 4 to see which limit is reached first.

## 7. Releasing a change

1. Run the tests (`pytest`; add `TEST_POSTGRES_URL=...` against a scratch Postgres to cover the database-specific paths).
2. If `app/models.py` changed, make sure there is a migration; the tests fail if not.
3. Apply migrations once, before new instances start: `python -m app.migrate` (a Cloud Run job using the same image), with `AUTO_MIGRATE=false` on the instances.
4. Deploy. Watch the dashboard for 15 minutes: errors, latency, cache hit ratios (which start cold).
5. Roll back by redeploying the previous image. Migrations are written to be reversible (`alembic downgrade -1`), but prefer rolling forward with a fix unless data is at risk.
