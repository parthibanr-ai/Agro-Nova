# Backend settings reference (`backend/.env`)

Shared by the [mobile](user_manual_mobile.md) and [web](user_manual_web.md) manuals. The backend reads `backend/.env`; **restart uvicorn after changing it**. `.env` holds secrets and is git-ignored; never commit it. Every value is optional for local development, and the app degrades gracefully.

## Core

| Setting | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./agrin.db` | Where plots and soil samples are stored. SQLite is a local file. For production use Postgres (add the driver to requirements), e.g. `postgresql+psycopg://user:pass@host/agrin`. |
| `CORS_ORIGINS` | `http://localhost:8080,http://localhost:5173` | Web addresses allowed to call the API from a browser. **Web app only** (the Android app is not a browser and ignores it). Add your deployed web address here. |
| `DEFAULT_COUNTRY` | `IN` | Country given to a new user until they choose one: `IN`, `BR`, `RU` or `CN`. |

## Login and admin

| Setting | Meaning |
|---|---|
| `AUTH_MODE` | `dev`: the backend trusts an `X-Dev-User` header, so any name counts as logged in. The app sends `dev-farmer`. Local testing only; anyone who can reach the server can impersonate anyone. `firebase`: farmers sign in with Firebase Authentication and the backend verifies their signed token. Use in production. |
| `FIREBASE_CREDENTIALS_PATH` | Used when `AUTH_MODE=firebase` and for push notifications. Path to the service-account JSON key from Firebase (Project settings > Service accounts). Keep it in `backend/secrets/` (git-ignored). |
| `ADMIN_API_KEY` | Password for `POST /api/v1/admin/notifications/dispatch`, which sends scheme, weather and market push notifications. A scheduler calls it with header `X-API-Key: <value>`. Replace `change-me` with a long random string before deploying: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

## Google Earth Engine (satellite data)

Without it, weather and rainfall history come from Open-Meteo; you lose satellite vegetation (NDVI), soil moisture and the 10-year rainfall comparison.

| Setting | Meaning |
|---|---|
| `EE_AUTH_MODE` | `user` for local development, `service_account` for servers. |
| `GEE_CLOUD_PROJECT` | Your Google Cloud **project ID** (e.g. `my-agrin-123`), registered for Earth Engine. |
| `GEE_SERVICE_ACCOUNT_EMAIL`, `GEE_SERVICE_ACCOUNT_KEY_PATH` | Server login (`service_account` mode only). |

**Local setup (`user` mode)**

1. Register your project at https://code.earthengine.google.com/register (choose *Unpaid usage* for non-commercial, *Paid* for commercial) and make sure the **Google Earth Engine API** is enabled on it.
2. Install the library and log in once:
   ```powershell
   cd D:\AgriN\backend
   .venv\Scripts\pip install earthengine-api
   .venv\Scripts\earthengine authenticate
   ```
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
| `GEMINI_API_KEY` | Create at https://aistudio.google.com/apikey. Without it `/diagnosis` returns 503; the rest of the app works. |
| `GEMINI_MODEL` | Model to use (default `gemini-2.5-flash`). |
| `GEMINI_USE_VERTEX`, `GCP_LOCATION` | Use Gemini through Vertex AI in your Google Cloud project instead of an API key. |

## Cloud Translation (advice in the farmer's language)

| Setting | Meaning |
|---|---|
| `TRANSLATE_API_KEY` | Translates advice, remedies, schemes and notifications into the chosen language. Without it that content stays English (fixed screen labels are still translated). |

To get a key: Cloud Console > enable **Cloud Translation API** (billing must be on; there is a free monthly allowance) > APIs & Services > Credentials > Create credentials > API key > restrict it to *Cloud Translation API* only. Paste into `.env` with no quotes or spaces. Test: `GET /api/v1/schemes?lang=hi` should return Hindi text. The backend caches each translated string, so repeats cost nothing. Bodo, Kashmiri, Sanskrit and Santali may not be covered and fall back to Hindi or Urdu.

## Checking what is active

`GET /api/v1/health` shows whether Earth Engine is connected. Everything else fails soft: a missing key disables only its own feature.
