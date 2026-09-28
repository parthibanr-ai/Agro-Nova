# Agro Nova - Regenerative Agricultural Intelligence

Web + mobile app (one Flutter codebase) with a FastAPI backend on Google Cloud. For farmers in India, and
extensible to Brazil, Russia and China (BRIC).

| Requirement | Where it lives |
|---|---|
| Four-corner plot capture (map tap, GPS, typed lat/lon, or search a place), server validation | `app/lib/screens/plot_capture_screen.dart`, `backend/app/domain/polygon.py`, `backend/app/services/geocode.py` |
| Soil data from public sources; prompt to enter existing govt data or how to get it tested; sample lat/lon | `backend/app/services/soil.py`, `app/lib/screens/soil_screen.dart` |
| Satellite-driven, crop-personalised weather outlook (GEE, extensible) | `backend/app/providers/`, `backend/app/services/forecast.py` |
| El Nino / La Nina in forecasting | `backend/app/services/enso.py` + country teleconnections in `data/countries.json` |
| Plant photo -> deficiency/pest/disease -> organic remedy + why organic beats chemical | `backend/app/services/diagnosis.py` (vision model: OpenAI, Anthropic or Gemini), `data/remedies.json` |
| Government schemes/subsidies + push | `data/schemes.json`, `services/notifications.py` (FCM) |
| LLM next-sowing-season crop recommendation, using the plot's soil/satellite/ENSO data and the country's local season names (Kharif/Rabi/Zaid, Safra/Safrinha, ...), refined by Indian state | `services/crop_recommendation.py` (LLM via `services/gemini_client.py`) |
| Self-resilient farming - generic compost/milk/biogas plan, plus an LLM section hyper-personalised to the plot's own soil/climate/ENSO and current crop | `services/advice.py`, `data/resilience.json`, `services/personalized_advice.py` (Gemini) |
| Water and resource saving - generic tips, plus an LLM section personalised to the plot's actual rainfall deficit/surplus and soil moisture | `data/water_tips.json`, `services/personalized_advice.py` (Gemini) |
| Value addition + market access - generic ideas, plus an LLM section personalised to the plot's crop, livestock and coming season | `data/value_add.json`, `services/personalized_advice.py` (Gemini) |
| 22 Indian languages + Portuguese, Russian, Chinese | `backend/app/core/languages.py`, `app/lib/l10n`, `services/translation.py` |
| Model vendors in order OpenAI -> Anthropic -> Gemini, each with a circuit breaker; a vendor with no key is skipped | `services/gemini_client.py`, `LLM_ORDER` |
| Privacy (India's DPDP Act): versioned notice, a choice per purpose, consent history, export, erasure, retention | `services/consent.py`, `data/privacy_notice.json`, `services/account.py`, `batch/retention.py`, `app/lib/screens/consent_screen.dart` |
| Works with poor connectivity: saved answers, plots and soil samples queued offline and sent later without duplicates | `app/lib/core/offline.dart`, `app/lib/core/app_state.dart` |
| "Answer ready" push that opens the finished result | `services/jobs.py` (`_notify`), `app/lib/core/push_links.dart`, `app/lib/screens/push_router.dart` |
| Built to scale: AI screens as jobs, Redis shared cache and limits, Cloud Tasks, nightly Earth Engine snapshots, App Check | `services/jobs.py`, `core/shared_store.py`, `services/tasks.py`, `batch/climate_snapshot.py`, `core/auth.py` |
| Metrics, alert rules, and a local Prometheus + Grafana dashboard | `core/metrics.py`, `ops/alerts.yml`, `ops/monitoring/` |
| Big data | `pipelines/export_climatology.py` (Earth Engine -> BigQuery) |

Design and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Running it in production: [docs/OPERATIONS.md](docs/OPERATIONS.md). Every backend setting: [docs/env_reference.md](docs/env_reference.md).

## Run the backend

```powershell
cd backend
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
copy .env.example .env         # AUTH_MODE=dev works without any Google credentials
.venv\Scripts\python -m pytest # backend tests (SQLite in memory; Postgres and Redis ones run when TEST_POSTGRES_URL / TEST_REDIS_URL are set)
.venv\Scripts\uvicorn app.main:app --reload   # http://localhost:8000/docs (send header X-Dev-User: me)
```

Without credentials the API still works: weather comes from Open-Meteo, soil from ISRIC SoilGrids, and
place search from OpenStreetMap Nominatim. Add credentials to unlock the Google services:

- `GEE_CLOUD_PROJECT` (+ `earthengine authenticate` or a service account): satellite NDVI, CHIRPS, ERA5-Land, SMAP.
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (or `GEMINI_USE_VERTEX=true`): the AI features (plant diagnosis, crop recommendation, personalised advice). They are tried in the order of `LLM_ORDER` (default `openai,anthropic,gemini`); a vendor without a key is skipped, so one key is enough. With none, the AI endpoints return 503.
- `TRANSLATE_API_KEY`: server-side translation of all dynamic content. Without it content is English.
- `AUTH_MODE=firebase` + `FIREBASE_CREDENTIALS_PATH`: Firebase Auth and FCM push.

## Privacy, monitoring and production

- **Privacy (DPDP):** the app asks what the farmer allows before anything else. `CONSENT_MODE=off|monitor|enforce` decides whether the server also refuses work without consent; `RETENTION_DAYS` and `python -m app.batch.retention` erase inactive accounts. Set `DATA_CONTROLLER_NAME`, `GRIEVANCE_OFFICER_NAME` and `GRIEVANCE_OFFICER_EMAIL`, which the notice shows. The notice wording and retention period need legal review before launch.
- **Monitoring:** `GET /metrics` (admin key) exposes Prometheus metrics. For a local Prometheus + Grafana dashboard, run `docker compose up -d` in [ops/monitoring](ops/monitoring/README.md).
- **Production:** `AUTH_MODE=firebase` needs a real `ADMIN_API_KEY` (the server refuses to start otherwise) and Postgres; Redis, Cloud Tasks and the nightly satellite job are optional and described in [docs/OPERATIONS.md](docs/OPERATIONS.md). SQLite is the development default.

## Run the app

Detailed, platform-specific steps (emulator setup, the Pixel_10 window-fit quirk, permissions, APK builds,
troubleshooting) are in [docs/user_manual_mobile.md](docs/user_manual_mobile.md) and
[docs/user_manual_web.md](docs/user_manual_web.md). Quick start:

```powershell
cd app
flutter pub get
flutter gen-l10n
flutter run -d chrome --dart-define=API_BASE_URL=http://localhost:8000   # web
flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8000              # Android emulator
```

The Android Google Maps key is never hardcoded: set the `MAPS_API_KEY` environment variable (a key with
**Maps SDK for Android** enabled, restricted to this app) before running `flutter run`/`flutter build` -
`android/app/build.gradle.kts` injects it into the manifest at build time. iOS (`AppDelegate.swift`) and
web (`index.html`) still need their own Maps keys added. Run `flutterfire configure` for Firebase.
Translate the remaining UI languages with `python tools/translate_arb.py` (needs `TRANSLATE_API_KEY`).
