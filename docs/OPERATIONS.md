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

**Try it locally.** [../ops/monitoring/](../ops/monitoring/README.md) is a Docker Compose stack (Prometheus, Grafana, a ready-made dashboard) that scrapes an API running on your machine.

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
| `agrin_shared_store_enabled`, `agrin_shared_store_up` | Redis configured on this instance, and usable right now |
| `agrin_shared_store_errors_total`, `agrin_shared_cache_total{cache,result}` | Failed Redis calls; lookups in the shared cache layer |
| `agrin_climate_snapshot_age_days` | Days since the newest nightly satellite snapshot (-1 if none) |
| `agrin_app_check_total{result}` | App Check tokens seen: `valid`, `missing`, `invalid` (when `APP_CHECK_MODE` is not `off`) |
| `agrin_accounts_deleted_total` | Farmer accounts erased on request |

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
*Alert: AgroNovaAdviceCacheColdSpend.* Fewer than half of AI advice requests are served from cache. Expected right after a restart or a new region going live. If it persists, check that the app version is not sending unusual inputs, that `CACHE_ENABLED` is not `false`, and that `AI_CACHE_TTL_S` is not tiny. Without `REDIS_URL` each instance keeps its own cache, so more instances lower the ratio; set up the shared store (section 6.1) at that scale. With Redis on, check `AgroNovaSharedStoreDown` first.

### Database connections exhausted
*Alert: AgroNovaDbPoolSaturated.* An instance uses over 90% of its pool; at 100% requests wait `DB_POOL_TIMEOUT_S` and then fail. Find what holds connections (slow queries, long transactions) before raising `DB_POOL_SIZE`. Remember the database has a global limit: instances x (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) must stay below it, otherwise put PgBouncer in front and set `DB_PGBOUNCER=true`.

### Rate limit spike
*Alert: AgroNovaRateLimitSpike.* Many requests are being refused with 429. Look at the `scope` label: `ai-min` or `diagnosis-min` from a few farmers means a stuck or aggressive client (an app version retrying in a loop); `ip-min` means one address is flooding the API. Blocking an address is done at the load balancer or with Cloud Armor, not in the app. If honest farmers are hitting the limits, raise `AI_RATE_PER_MINUTE` or `IP_RATE_PER_MINUTE` (carrier-grade NAT puts many farmers behind one address).

## 6. Setting up the shared store, the queues and the nightly job

None of this is needed for one instance or a pilot: the defaults (`REDIS_URL` empty, `TASK_BACKEND=local`, `JOB_BACKEND=local`, no nightly job) run everything inside each instance. Turn each piece on when the fleet grows. All of it degrades safely: a piece that is switched on but failing falls back to the local behaviour.

### 6.1 Shared cache and rate limits (Redis)

1. Create a Memorystore for Redis instance (Standard tier for failover; single node, **not** cluster mode, because the rate-limit script touches several keys at once) in the same region and VPC as the service, and give Cloud Run a Serverless VPC connector or Direct VPC egress to reach it.
2. Set `REDIS_URL=redis://<ip>:6379/0` on every instance. Nothing else changes.

What moves into Redis: the weather, forecast, satellite, soil, geocode, advice and translation caches (one instance computes, the rest read; a lock stops several instances asking the same upstream at once) and the rate-limit counters (limits become exact across the fleet). Values are stored as JSON, never pickle. A slow or dead Redis costs a request at most `REDIS_SOCKET_TIMEOUT_S` (0.3 s) once, then it is skipped for `REDIS_DOWN_BACKOFF_S` (15 s) and each instance carries on with its own memory. Size it for the translation cache: 200,000 entries at about 1 KB each is 200 MB, so start with 1 to 2 GB and an `allkeys-lru` eviction policy.

Verified against a real Redis 7 (`TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/test_shared_store.py`) as well as fakeredis. Not yet load-tested with many instances.

### 6.2 Cloud Tasks (fan-out and AI jobs on any instance)

`TASK_BACKEND=cloudtasks` sends the push-notification fan-out to Cloud Tasks; `JOB_BACKEND=cloudtasks` does the same for AI jobs, so any instance can run any job and a burst is absorbed by the queue instead of the accepting instance.

```
gcloud tasks queues create agrin-tasks --location=asia-south1 --max-dispatches-per-second=50 --max-concurrent-dispatches=100
gcloud tasks queues create agrin-jobs  --location=asia-south1 --max-dispatches-per-second=200 --max-concurrent-dispatches=400 --max-attempts=3
gcloud iam service-accounts create agrin-queue-caller
# lets the API create tasks, and lets it name that account as the caller
gcloud projects add-iam-policy-binding PROJECT --member=serviceAccount:<api service account> --role=roles/cloudtasks.enqueuer
gcloud iam service-accounts add-iam-policy-binding agrin-queue-caller@PROJECT.iam.gserviceaccount.com --member=serviceAccount:<api service account> --role=roles/iam.serviceAccountUser
gcloud run services add-iam-policy-binding agrin-api --member=serviceAccount:agrin-queue-caller@PROJECT.iam.gserviceaccount.com --role=roles/run.invoker
# AI job photos wait here (a task body is limited to 100 KB): private, same region, 1-day lifecycle rule
gcloud storage buckets create gs://PROJECT-agrin-job-payloads --location=asia-south1 --uniform-bucket-level-access
gcloud storage buckets update gs://PROJECT-agrin-job-payloads --lifecycle-file=lifecycle-1day.json
gcloud storage buckets add-iam-policy-binding gs://PROJECT-agrin-job-payloads --member=serviceAccount:<api service account> --role=roles/storage.objectAdmin
```

Settings: `TASK_TARGET_URL` (the service's public https address), `TASK_SERVICE_ACCOUNT` (`agrin-queue-caller@...`), `CLOUD_TASKS_PROJECT`, `JOB_PAYLOAD_BUCKET`; `CLOUD_TASKS_QUEUE` and `CLOUD_TASKS_JOB_QUEUE` default to the queue names above. The service refuses to start if a backend is switched on without the settings it needs. Cloud Tasks calls `POST /api/v1/internal/tasks/{name}` with an OIDC token for the queue account; the endpoint checks the token's signature, audience and account, and still accepts the admin key for operators. Tasks are delivered at least once and every handler is safe to run twice. If a task cannot be queued, notification tasks run on the local instance instead and an AI job answers "busy, try again" at once. Set the Cloud Run request timeout above your slowest AI job (300 s is plenty).

**Tested with fakes only.** The adapter, the authentication and the whole job flow are covered by `tests/test_cloud_tasks.py` against a fake queue and a fake bucket. It has not been run against real Cloud Tasks or Cloud Storage: do that in staging first (run one diagnosis with a photo and check that the task runs, the object appears, and it is deleted).

### 6.3 Nightly satellite snapshots (Earth Engine, in bulk)

Earth Engine answers one plot in 10 to 30 s, but a thousand plots in one request in about 30 s. `python -m app.batch.climate_snapshot` reads every plot, asks Earth Engine for a chunk at a time (`EE_BATCH_CHUNK`, default 1000) and writes one row per plot to `climate_snapshots`. The API then reads that row (about 1 ms) instead of calling Earth Engine, and this also works on instances that have no Earth Engine credentials at all.

Measured on real Earth Engine, plots within a 40 km square in Punjab: 3 plots one at a time 22 s; the same 3 in one request 5 s; 500 plots 22 s; 1,000 plots 29 s; 2,500 plots 34 s. That is 0.013 to 0.04 s per plot instead of 7 s. The values matched the one-plot query within 2% (`TEST_EARTH_ENGINE=1 pytest tests/test_climate_snapshots.py`). Plots spread across a whole country need more satellite scenes per request than these clustered ones, so expect the real figure to be slower; measure it on a sample before planning the night's window.

Run it as a Cloud Run job (same image, command `python -m app.batch.climate_snapshot`, Earth Engine credentials, database access) from Cloud Scheduler each morning, after the day's ERA5 data is in. It is safe to run twice or to stop halfway. To spread a big run over several containers, run N copies with `--shard 0/N` to `--shard N-1/N`; mind Earth Engine's limit on concurrent requests for the project. If a chunk fails it is halved until the failing plot is found and skipped (its id is in the printed summary), so one bad polygon cannot stop the night. Snapshots of deleted or redrawn plots are purged after 7 days.

`EE_LIVE_FALLBACK` decides what a plot with no snapshot yet (added today) gets: `true` (default) is one live Earth Engine call for that plot; `false` is shared Open-Meteo weather until tonight's run. Use `false` at national scale so a burst of new plots cannot exhaust Earth Engine's quota. Snapshots older than `EE_SNAPSHOT_MAX_AGE_DAYS` (2) are ignored, and the alert "Satellite snapshots stale" fires.

`BIGQUERY_TABLE=project.dataset.table` (a table with the columns of `climate_snapshots`) also appends every night's rows to BigQuery for analytics and for training the seasonal model. The API does not read BigQuery: a per-request BigQuery query costs money and takes about a second, while a primary-key read in the app database costs neither. The BigQuery write has not been run against a real table. At tens of millions of plots the compute step becomes an Earth Engine batch export (`Export.table.toBigQuery`) loaded into the same table; nothing on the API side changes.

### 6.4 Firebase App Check (stop scripted accounts)

Anonymous sign-in lets a script create unlimited accounts and spend Gemini quota. App Check makes the app prove it is the genuine build. Roll it out in three steps so no real farmer is locked out:

1. In the Firebase console enable App Check for the Android app (Play Integrity), the iOS app (App Attest) and the web app (reCAPTCHA v3). For debug builds register the debug token the app prints. Build the web app with `--dart-define=RECAPTCHA_SITE_KEY=...`.
2. Release this version of the app (it sends the token whenever App Check works on the device) and set `APP_CHECK_MODE=monitor` on the server. Every request is counted in `agrin_app_check_total{result="valid|missing|invalid"}` and none is refused.
3. When `valid` is above roughly 95% of traffic for a few days (old app versions are the `missing` ones), set `APP_CHECK_MODE=enforce`. Requests without a valid token then get 403 "This app could not be verified". The alert "Unverified app traffic" watches the ratio in both modes.

Only endpoints that need a signed-in farmer are checked; `/health`, `/meta/*` and the admin and task endpoints are not. Not tested on a device or against the Firebase console: the server logic is unit-tested with the verification faked, and the Flutter side is compiled and unit-tested.

### 6.5 Erasing a farmer's data

`DELETE /api/v1/me?confirm=true` (the app's menu, "Delete my data") removes the farmer's profile, device token, plots, soil samples, satellite snapshots and AI jobs, and their Firebase sign-in account, and logs a hash of the id as a record that it happened. `GET /api/v1/me/export` returns everything stored about them. Not covered by the endpoint: Cloud Logging entries (request ids only; retention is a log setting), database backups (they age out on the backup schedule; say so in the privacy notice), and anything a deployment adds elsewhere. Whether this satisfies the DPDP Act's obligations (notice, consent, retention limits, grievance officer) is a legal question for the operator; the endpoint is the technical part.

### Shared store down
*Alert: AgroNovaSharedStoreDown.* `REDIS_URL` is set but Redis is failing on some instance, which is running on its own memory meanwhile. Nothing breaks: caches start cold (more upstream calls, watch Gemini and Earth Engine usage) and rate limits count per instance (up to N times looser). Check Memorystore's status, the VPC connector and whether the instance ran out of memory (`used_memory` against `maxmemory`; use `allkeys-lru`). The API retries Redis every `REDIS_DOWN_BACKOFF_S` seconds by itself.

### Satellite snapshots stale
*Alert: AgroNovaSnapshotStale.* The newest row in `climate_snapshots` is more than two days old, so the API has stopped using them and falls back to live Earth Engine calls (slow, and a quota risk at scale) or, with `EE_LIVE_FALLBACK=false`, to weather without satellite data. Check the Cloud Run job's last executions and logs for `python -m app.batch.climate_snapshot`: expired Earth Engine credentials, the project's Earth Engine quota, or the database. Run it by hand once with `--limit 50` to see the error, then without to catch up.

### Unverified app traffic
*Alert: AgroNovaUnverifiedAppTraffic.* More than 20% of requests carry no valid App Check token. In `monitor` mode this means it is not yet safe to `enforce` (old app versions, or a store build App Check does not recognise, for example an APK installed from outside Play); in `enforce` mode those farmers are being refused right now, so drop back to `monitor` first and investigate. A spike of `invalid` from few addresses is a script probing the API and needs no action beyond `enforce`.

## 7. Load testing

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

## 8. Releasing a change

1. Run the tests (`pytest`; add `TEST_POSTGRES_URL=...` against a scratch Postgres to cover the database-specific paths).
2. If `app/models.py` changed, make sure there is a migration; the tests fail if not.
3. Apply migrations once, before new instances start: `python -m app.migrate` (a Cloud Run job using the same image), with `AUTO_MIGRATE=false` on the instances.
   The service also refuses to start with `AUTH_MODE=firebase` and a default or short `ADMIN_API_KEY`, or with a queue backend switched on without its settings: a failed start after a deploy usually means one of those.
4. Deploy. Watch the dashboard for 15 minutes: errors, latency, cache hit ratios (which start cold).
5. Roll back by redeploying the previous image. Migrations are written to be reversible (`alembic downgrade -1`), but prefer rolling forward with a fix unless data is at risk.
