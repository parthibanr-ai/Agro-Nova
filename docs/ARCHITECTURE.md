# AgriN architecture

## 1. Shape

```
Flutter (Android / iOS / Web)  --HTTPS+Firebase ID token-->  FastAPI on Cloud Run
   Google Maps, GPS, camera                                    |-- Earth Engine (satellite/climate)
   Firebase Auth, FCM, App Check                               |-- LLMs in order OpenAI -> Anthropic -> Gemini
   ARB UI strings (26 langs)                                   |-- Cloud Translation (fixed text)
   Saved answers + offline write queue (on the phone)          |-- NOAA CPC ONI (El Nino/La Nina)
   Privacy notice + consent screen                             |-- ISRIC SoilGrids, Open-Meteo (keyless fallbacks)
                                                               |-- Postgres (Cloud SQL) / SQLite: users, plots, soil,
                                                               |     consent history, jobs, nightly satellite snapshots
                                                               |-- Redis (optional): shared caches, rate limits, locks
                                                               |-- Cloud Tasks (optional): AI jobs and push fan-out
                                                               |-- /metrics -> Prometheus -> Grafana, alerts (ops/)
Earth Engine --nightly batch--> climate_snapshots (API reads a row) and BigQuery --> Vertex AI (seasonal model, phase 2)
```

The backend follows the pattern from `D:\Hydro_sensing`: a swappable provider registry
(`providers/registry.py`), server-side polygon validation as the single source of truth, and Earth Engine
initialisation that reports failure via `/health` instead of crashing. Unlike Hydro_sensing, every external
dependency degrades gracefully so the API is usable with zero credentials.

## 2. Feature design

**Plot capture.** 4-12 corners in walking order; the server rejects self-intersecting shapes, plots <50 m2
or >10 km2, and coordinates outside the selected country (catches swapped lat/lon). Area is geodesic (WGS84).

**Soil.** Order of trust: farmer/lab/Soil Health Card sample (with its own lat/lon) -> ISRIC SoilGrids
(250 m modelled). Every response includes a prompt: enter existing government data, or the per-country steps
and official portals for getting soil tested. Indian users are pointed to the Soil Health Card portal, KVK
and Bhuvan.

**Hyper-personalised outlook.** `forecast.py` is a transparent rule engine: observed rainfall/NDVI/soil
moisture vs. historic normal (GEE), 10-16 day forecast, crop growth stage from sowing date and per-crop
heat/frost/water thresholds, plus the ENSO advisory. Rules are explainable to a farmer. `ObservedClimate`
and the report `features` are the hook for an ML model in Vertex AI trained on the BigQuery climatology.

**El Nino / La Nina.** Phase and strength from NOAA's ONI (cached 24 h). Country teleconnections are data
(`countries.json -> enso_impact`); crop sensitivity is data (`crops.json`). Advisory severity = crop
sensitivity, downgraded for weak events. It is a seasonal tendency, and says so.

**Plant diagnosis.** A vision model (whichever vendor in `LLM_ORDER` answers first) classifies the photo, constrained to the ids in `remedies.json`. The remedy,
preparation, prevention and the "why organic vs. chemical" text come from the curated KB, never from the
model, so advice stays organic-first and consistent. Low confidence returns "retake the photo / see your KVK".

**Schemes and push.** Schemes are curated per country with an official URL; the API filters by
country/state/crop/farm size. `notifications.py` groups farmers into audiences (country, state, first crop,
language), builds each audience's digest once (new schemes, ENSO/weather advisory, value-addition nudge),
localises it once and sends it 500 devices per FCM call; run it from Cloud Scheduler calling
`POST /api/v1/admin/notifications/dispatch?dry_run=false`. See section 6.

**Resilience, water, market.** Livestock calculator (milk income, feed cost, dung -> compost, biogas,
manure self-sufficiency of the plot), an integrated-farming roadmap, water tips prioritised by crop and
ENSO state, and per-crop value-addition ideas with market channels per country.

**Model vendors.** Every AI call goes through `services/gemini_client.py`. `LLM_ORDER` (default `openai,anthropic,gemini`)
lists the vendors to try; the first that answers wins, and a vendor with no key is skipped, so a deployment with only a Gemini
key behaves as before. Any failure (overload, exhausted quota, timeout, a reply that is not JSON) moves to the next vendor.
OpenAI and Anthropic are called over plain HTTP, get the plant photo in their own image format, and each has a circuit breaker
like the Gemini models, so a vendor that is down costs one wasted call, not one per request. A Gemini 429 (quota exhausted)
moves straight to the next model instead of retrying. Which vendor answered is visible in `agrin_gemini_calls_total{model=...}`.

**Privacy and consent (DPDP).** The app shows a versioned privacy notice (`data/privacy_notice.json`, served and translated by the
API) before anything else, and records one choice per purpose: `service` (plots and farm details, required), `ai` (plot conditions
and photos sent to the AI vendors) and `notifications` (device token). Nothing is pre-ticked; the choices can be changed or
withdrawn from "Privacy and my data", which also offers the data download and erasure. Current choices live on the user row,
every change is an append-only `consent_events` row, and both go with the export and the erasure. `CONSENT_MODE`
(`off`, `monitor`, `enforce`) decides whether the server refuses plot and soil writes, AI features and push registration
without the matching consent. Changing the notice's meaning means bumping its version, which asks every farmer again.
`python -m app.batch.retention` erases accounts unused for `RETENTION_DAYS` (`users.last_seen_at`).

**Offline and notifications.** The app remembers the last answers it saw (bounded, on the phone, per language) and shows them,
with a banner, when the server cannot be reached. Plots and soil samples made offline are queued on the phone with a
`client_ref`; the server returns the record it already has for a repeated `client_ref`, so a retry never duplicates. The queue
sends on a timer, on returning to the app and when the connection comes back, stops at the first sign of no connection, and drops
a change the server refuses for good. AI answers are not queued (they need the server). The "answer ready" push carries
`job_id`, `kind` and `plot_id`; tapping it opens that screen with the finished result, or asks afresh if the server no longer
holds it (results are kept about an hour).

## 3. Languages

Static UI: ARB files (`app/lib/l10n`), generated by `flutter gen-l10n`. Hand-written: en, hi, ta, pt, ru, zh;
the rest via `tools/translate_arb.py`. Dynamic content: for the widely used languages the AI vendor writes advice directly in
the farmer's language (marked in the response so it is not translated twice); a middleware translates all other human-readable
strings in any JSON response to `?lang=` using Cloud Translation, skipping ids/urls/dates, caching per string. The privacy notice
is translated this way, and its translations need native-speaker and legal review.
Bodo, Kashmiri, Sanskrit and Santali are flagged `machine_translation=false` and fall back to Hindi/Urdu
until Cloud Translation coverage is verified. Urdu, Sindhi and Kashmiri render right-to-left.

## 4. BRIC extension

A country is data + optional provider: add a block to `countries.json` (bbox, agencies, soil-testing
steps, ENSO impact), schemes to `schemes.json`, and market channels to `value_add.json`. India, Brazil,
Russia and China are seeded. Country-specific satellite/soil sources (Bhuvan, INMET, Embrapa, Roshydromet,
CMA) plug in as `ClimateProvider` implementations.

## 5. Caveats and what must happen before launch

1. **Verify content with domain experts.** Schemes, remedy doses, ENSO impacts and livestock economics
   are seeded from general knowledge, not audited. Have agronomists and each country's extension services
   review `backend/app/data/*.json`. Scheme entries need a sync job against official feeds (PIB, myScheme, gov.br).
2. **Machine translation of farm advice** must be reviewed by native speakers.
3. **Bhuvan** has no stable public API; integrate through a data-sharing agreement/WMS as ISRO permits.
4. **Earth Engine** commercial use requires the appropriate Cloud licence.
5. Secure production: `AUTH_MODE=firebase`, a real `ADMIN_API_KEY` (the server refuses to start without one), Postgres,
   `APP_CHECK_MODE=monitor` then `enforce` (rolled out as in OPERATIONS.md 6.4). Per-farmer rate limits exist.
6. Privacy (India's DPDP Act): the app shows the privacy notice and records a choice per purpose; `CONSENT_MODE` decides whether the
   server enforces it; inactive accounts are erased after `RETENTION_DAYS`; export and erasure exist. The notice text, the retention
   period and the grievance contacts still need legal review (OPERATIONS.md sections 6.5 to 6.7).
7. Offline: the app shows saved answers without signal and queues plots and soil samples for later, safely repeatable, sending (section 6, OPERATIONS.md 6.8).

## 6. Scaling and operations

The design goal is that the cost of serving a farmer is a cache lookup, because nearly everything is shared by
farmers in the same area. What each layer does:

- **Requests never wait on a slow upstream while holding a thread or a database connection.** Slow requests
  (crop recommendation, photo diagnosis, the resilience, water and market advice, the forecast) are jobs: the API
  answers at once (or within about 2 s if the result is cached) and the app polls `GET /jobs/{id}`. A farmer who
  leaves the app while waiting gets a push when it is ready. Jobs are rows in the database, so any instance can
  answer a poll. By default the work runs on the accepting instance's bounded worker pool, and a job whose instance
  dies is failed after a lease so the app retries; with `JOB_BACKEND=cloudtasks` the job is queued on Cloud Tasks
  (the photo waits in a Cloud Storage bucket) and any instance can run it. Calls to Gemini are capped per instance (`GEMINI_MAX_CONCURRENCY`), retried within a
  time budget, and paused by a circuit breaker when Gemini is overloaded. (`services/jobs.py`, `gemini_client.py`)
- **Shared work is done once.** Weather history and forecasts are cached per ~5 km cell, SoilGrids per ~250 m
  cell, and Gemini advice is shared by farmers with the same state, crop, language and rounded conditions.
  Concurrent requests for the same missing item make one upstream call, and with `REDIS_URL` set the caches, that
  single flight and the rate-limit counters are shared by the whole fleet (`core/cache.py`, `core/shared_store.py`,
  `services/advice_cache.py`, `providers/registry.py`).
- **Satellite data is computed overnight, not per request.** `python -m app.batch.climate_snapshot` asks Earth
  Engine about a thousand plots per request and stores one row per plot; the API reads the row in about 1 ms. (An
  Earth Engine call for one plot takes 10 to 30 s.)
- **Advice is written in the farmer's language by Gemini** for the widely used languages, so the paid translation
  API only handles fixed text, which is translated once and shared.
- **Push notifications are computed per audience, not per farmer** and sent in batches, with a database claim per
  audience-day so retries never send twice. `users.primary_crop` plus a partial index make each page of an
  audience a short index scan. Measured on Postgres 16: 1,000,000 farmers plan and send in about 17 s with FCM
  faked (the first version, which recomputed each farmer's first plot per page, took 104 s for 200,000).
  (`services/notifications.py`, `services/tasks.py`)
- **The schema is migrated, not recreated** (`migrations/`, Alembic). SQLite migrates itself at start-up; Postgres
  runs `python -m app.migrate` once per release. A test fails if the models and migrations disagree.
- **Limits and visibility.** Per-farmer and per-IP rate limits on AI endpoints; Prometheus metrics at
  `/metrics`; request ids on every log line; liveness and readiness probes; alert rules and runbooks in
  [OPERATIONS.md](OPERATIONS.md); a CI gate that fails if a change makes more upstream calls per farmer. `ops/monitoring/` is a
  Docker Compose stack (Prometheus with the alert rules, Grafana with a ready dashboard: which vendor answers, slowest routes,
  jobs, caches, queue, database connections) for watching a local or staging API.

**What is still open for a national rollout.** The shared store, the queues, the nightly satellite job and App Check
are built and tested, but Cloud Tasks, Cloud Storage, BigQuery and App Check have only been exercised against
fakes and the Firebase console setup is manual (OPERATIONS.md section 6), so each needs one staged run before
trusting it. Earth Engine's concurrent-request limit for the project bounds how fast the nightly job can go. The
weather history (Open-Meteo, six calls per cold cell) is now the slowest first-visit step and will need a paid or
self-hosted source at scale. Legal review of the DPDP notice, retention period and consent purposes, a device test of offline sync
and push deep links, and written confirmation of the licence and quota terms of Earth Engine, Gemini, OpenAI, Anthropic and the weather
sources are also open. None of
this has been load-tested at national volume; `loadtest/k6_farmers.js` is the tool for testing a staging deployment.

## 7. Roadmap

1. Now: this repo (backend and Flutter client tested; not yet deployed anywhere).
2. Wire Firebase and Maps keys, deploy a staging environment to Cloud Run, run the load test and the queue/App Check checks there.
3. Scheme sync job, voice input/output for low-literacy users, a faster weather-history source, DPDP legal sign-off.
4. Vertex AI seasonal model from BigQuery climatology; Bhuvan/national providers per country.
