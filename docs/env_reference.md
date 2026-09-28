# Backend settings reference (`backend/.env`)

Shared by the [mobile](user_manual_mobile.md) and [web](user_manual_web.md) manuals. The backend reads `backend/.env`; **restart uvicorn after changing it**. A real environment variable with the same name (for example a Windows user variable `GEMINI_API_KEY`) **overrides** `.env`, so if a key you just pasted is "invalid", check `echo $env:GEMINI_API_KEY` for a stale value. `.env` holds secrets and is git-ignored; never commit it. Every value is optional for local development, and the app degrades gracefully.

## Core

| Setting | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./agrin.db` | Where plots and soil samples are stored. SQLite is a local file. For production use Postgres (add the driver to requirements), e.g. `postgresql+psycopg://user:pass@host/agrin`. |
| `CORS_ORIGINS` | `http://localhost:8080,http://localhost:5173` | Web addresses allowed to call the API from a browser. **Web app only** (the Android app is not a browser and ignores it). Add your deployed web address here. |
| `DEFAULT_COUNTRY` | `IN` | Country given to a new user until they choose one: `IN`, `BR`, `RU` or `CN`. |

## Capacity, caching and limits

Defaults suit a single server. Tune them when many farmers use the API at once.

| Setting | Default | Meaning |
|---|---|---|
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S` | `10`, `20`, `10` | Database connection pool (Postgres etc.; ignored for SQLite). Keep instances x (pool size + overflow) under the database's connection limit; use a connection pooler such as PgBouncer for large fleets. |
| `CACHE_ENABLED` | `true` | Master switch for every in-process cache. Set `false` to debug stale data. |
| `CLIMATE_CACHE_TTL_S` | `21600` (6 h) | How long satellite/weather history is reused. Farms in the same ~5 km cell share one lookup per day; Earth Engine results are kept per plot. |
| `FORECAST_CACHE_TTL_S` | `3600` | How long a forecast is reused per ~5 km cell. |
| `SOIL_CACHE_TTL_S` | `2592000` (30 d) | How long SoilGrids values are reused (a static dataset, cached per ~250 m cell). |
| `AI_CACHE_TTL_S` | `86400` (24 h) | How long a Gemini answer is shared. Farmers in the same state with the same crop and rounded conditions (rain to 5-10 mm, temperature to 1 C, area to 0.5 ha) get the same answer, and it refreshes daily. Failed calls are never cached. |
| `TRANSLATION_CACHE_MAX_ENTRIES` | `200000` | Size limit of the translation cache (least recently used strings are dropped). |
| `RATE_LIMIT_ENABLED` | `true` | Per-farmer limits on Gemini-backed endpoints. Over the limit the API answers `429` with a `Retry-After` header. |
| `AI_RATE_PER_MINUTE`, `AI_RATE_PER_DAY` | `20`, `300` | Per farmer, for crop recommendation, resilience, water tips and market advice. |
| `DIAGNOSIS_RATE_PER_MINUTE`, `DIAGNOSIS_RATE_PER_DAY` | `5`, `40` | Per farmer, for photo diagnosis (the costliest call). |
| `IP_RATE_PER_MINUTE` | `600` | Per IP address, across those endpoints. High on purpose: mobile carriers put many farmers behind one IP, so this only stops one machine flooding the API. |
| `TRUST_FORWARDED_FOR` | `false` | Set `true` behind a proxy that adds `X-Forwarded-For` (Cloud Run does) so limits use the real client address. |

Caches and limit counters live in each server's memory, so with several instances each keeps its own (a limit of 20 per minute becomes up to 20 per instance). A shared store such as Redis would make them global; the code is arranged so that only `app/core/cache.py` and `app/core/ratelimit.py` would change.

## Login and admin

| Setting | Meaning |
|---|---|
| `AUTH_MODE` | `dev`: the backend trusts an `X-Dev-User` header, so any name counts as logged in. The app sends `dev-farmer`. Local testing only; anyone who can reach the server can impersonate anyone. `firebase`: farmers sign in with Firebase Authentication and the backend verifies their signed token. Use in production. |
| `FIREBASE_CREDENTIALS_PATH` | Used when `AUTH_MODE=firebase` and for push notifications. Path to the service-account JSON key from Firebase (Project settings > Service accounts). Firebase Authentication is not a separate API in the Library: add Firebase to your Cloud project at console.firebase.google.com, then Build > Authentication > Get started and enable **Anonymous** sign-in (the app signs in anonymously). Enabling it turns on the Identity Toolkit API, which must also be allowed on any API key the client uses. Keep it in `backend/secrets/` (git-ignored). |
| `ADMIN_API_KEY` | Password for `POST /api/v1/admin/notifications/dispatch`, which sends scheme, weather and market push notifications. A scheduler calls it with header `X-API-Key: <value>`. Replace `change-me` with a long random string before deploying: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

## Google Earth Engine (satellite data)

Without it, weather and rainfall history come from Open-Meteo; you lose satellite vegetation (NDVI), soil moisture and the 10-year rainfall comparison.

| Setting | Meaning |
|---|---|
| `EE_AUTH_MODE` | `user` for local development, `service_account` for servers. |
| `GEE_CLOUD_PROJECT` | Your Google Cloud **project ID** (e.g. `my-agrin-123`), registered for Earth Engine. |
| `GEE_SERVICE_ACCOUNT_EMAIL`, `GEE_SERVICE_ACCOUNT_KEY_PATH` | Server login (`service_account` mode only). |

**Local setup (`user` mode)**

1. Register your project at https://code.earthengine.google.com/register, or at `https://console.cloud.google.com/earth-engine/configuration?project=<your project id>` (choose *Unpaid usage* for non-commercial, *Paid* for commercial), and make sure the **Google Earth Engine API** is enabled on it. Enabling the API alone is not enough: the error "Project ... is not registered to use Earth Engine" means this registration step is missing.
2. Install the library and log in once:
   ```powershell
   cd D:\AgriN\backend
   .venv\Scripts\pip install earthengine-api
   .venv\Scripts\earthengine authenticate
   ```
   Log in with the Google account that owns (or has the **Service Usage Consumer** role on) the project. "Caller does not have required permission to use project ..." means the saved login is the wrong account; run `earthengine authenticate --force` and sign in again.
3. In `.env`: `EE_AUTH_MODE=user` and `GEE_CLOUD_PROJECT=<your project id>`.
4. Restart the backend and open http://localhost:8000/api/v1/health. `"earth_engine": true` means it works; otherwise `earth_engine_error` says why.

**Server setup (`service_account` mode)**

1. Cloud Console > IAM & Admin > Service Accounts > create one (e.g. `agrin-gee`), then register it for Earth Engine at the register page above and give it an Earth Engine Resource role.
2. Keys > Add key > JSON; save as `backend/secrets/gee-service-account.json`.
3. In `.env`:
   ```
   EE_AUTH_MODE=service_account
   GEE_SERVICE_ACCOUNT_EMAIL=agrin-gee@my-agrin-123.iam.gserviceaccount.com
   GEE_SERVICE_ACCOUNT_KEY_PATH=./secrets/gee-service-account.json
   GEE_CLOUD_PROJECT=my-agrin-123
   ```

The first forecast with Earth Engine active can take 10 to 30 seconds. If Earth Engine errors, the backend falls back to Open-Meteo automatically.

## Gemini (plant photo diagnosis)

| Setting | Meaning |
|---|---|
| `GEMINI_API_KEY` | Create at https://aistudio.google.com/apikey (newer keys start with `AQ.`; older ones with `AIza`). It must belong to a project with credits or billing, otherwise every call fails with "prepayment credits are depleted". This key is separate from the Maps and Translation keys, whose API restrictions do not include Gemini. Without it, `/diagnosis`, `/plots/{id}/crop-recommendation`, and the personalized sections of `/resilience`, `/water-tips` and `/plots/{id}/market` are unavailable; the rest of the app works. The free tier has a low daily request cap - enable billing on the project to lift it. |
| `GEMINI_MODEL` | Model to use (default `gemini-3.1-flash-lite`, the most reliable under Google's demand spikes). Older models such as `gemini-2.5-flash` return 404 "no longer available to new users" on new keys. A temporary 503 "high demand" can occur; retry. |
| `GEMINI_FALLBACK_MODEL` | Used when `GEMINI_MODEL` keeps answering 503 "high demand" or 429 (default `gemini-3.6-flash,gemini-flash-lite-latest`; a comma-separated list, tried in order). Each call tries the main model up to 3 times with increasing waits, then each fallback the same way, before returning a 502 to the app. |
| `GEMINI_USE_VERTEX`, `GCP_LOCATION` | Use Gemini through Vertex AI in your Google Cloud project instead of an API key. |

## Google Geocoding (place search fallback)

| Setting | Meaning |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Fallback for the plot-capture map's "search a place" box when OpenStreetMap Nominatim has no match (common for small Indian villages). Create in Cloud Console with **Geocoding API** enabled and added to this key's API restrictions. Without it, search still works via Nominatim alone, just with weaker coverage of small places. This is a separate, server-side key from the Android app's `MAPS_API_KEY` build-time environment variable (see the mobile manual), which renders the map itself. |

## Cloud Translation (advice in the farmer's language)

| Setting | Meaning |
|---|---|
| `TRANSLATE_API_KEY` | Translates advice, remedies, schemes and notifications into the chosen language. Without it that content stays English (fixed screen labels are still translated). |

To get a key: Cloud Console > enable **Cloud Translation API** (billing must be on; there is a free monthly allowance) > APIs & Services > Credentials > Create credentials > API key > restrict it to *Cloud Translation API* only. Paste into `.env` with no quotes or spaces. Test: `GET /api/v1/schemes?lang=hi` should return Hindi text. The backend caches each translated string, so repeats cost nothing. Bodo, Kashmiri, Sanskrit and Santali may not be covered and fall back to Hindi or Urdu.

## Checking what is active

`GET /api/v1/health` shows whether Earth Engine is connected. Everything else fails soft: a missing key disables only its own feature.
