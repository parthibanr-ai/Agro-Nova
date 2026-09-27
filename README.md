# Agro Nova - Regenerative Agricultural Intelligence

Web + mobile app (one Flutter codebase) with a FastAPI backend on Google Cloud. For farmers in India, and
extensible to Brazil, Russia and China (BRIC).

| Requirement | Where it lives |
|---|---|
| Four-corner plot capture (map tap, GPS, typed lat/lon, or search a place), server validation | `app/lib/screens/plot_capture_screen.dart`, `backend/app/domain/polygon.py`, `backend/app/services/geocode.py` |
| Soil data from public sources; prompt to enter existing govt data or how to get it tested; sample lat/lon | `backend/app/services/soil.py`, `app/lib/screens/soil_screen.dart` |
| Satellite-driven, crop-personalised weather outlook (GEE, extensible) | `backend/app/providers/`, `backend/app/services/forecast.py` |
| El Nino / La Nina in forecasting | `backend/app/services/enso.py` + country teleconnections in `data/countries.json` |
| Plant photo -> deficiency/pest/disease -> organic remedy + why organic beats chemical | `backend/app/services/diagnosis.py` (Gemini), `data/remedies.json` |
| Government schemes/subsidies + push | `data/schemes.json`, `services/notifications.py` (FCM) |
| LLM next-sowing-season crop recommendation, using the plot's soil/satellite/ENSO data and the country's local season names (Kharif/Rabi/Zaid, Safra/Safrinha, ...), refined by Indian state | `services/crop_recommendation.py` (Gemini) |
| Self-resilient farming - generic compost/milk/biogas plan, plus an LLM section hyper-personalised to the plot's own soil/climate/ENSO and current crop | `services/advice.py`, `data/resilience.json`, `services/personalized_advice.py` (Gemini) |
| Water and resource saving - generic tips, plus an LLM section personalised to the plot's actual rainfall deficit/surplus and soil moisture | `data/water_tips.json`, `services/personalized_advice.py` (Gemini) |
| Value addition + market access - generic ideas, plus an LLM section personalised to the plot's crop, livestock and coming season | `data/value_add.json`, `services/personalized_advice.py` (Gemini) |
| 22 Indian languages + Portuguese, Russian, Chinese | `backend/app/core/languages.py`, `app/lib/l10n`, `services/translation.py` |
| Big data | `pipelines/export_climatology.py` (Earth Engine -> BigQuery) |

Design and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Run the backend

```powershell
cd backend
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
copy .env.example .env         # AUTH_MODE=dev works without any Google credentials
.venv\Scripts\python -m pytest # 45 tests
.venv\Scripts\uvicorn app.main:app --reload   # http://localhost:8000/docs (send header X-Dev-User: me)
```

Without credentials the API still works: weather comes from Open-Meteo, soil from ISRIC SoilGrids, and
place search from OpenStreetMap Nominatim. Add credentials to unlock the Google services:

- `GEE_CLOUD_PROJECT` (+ `earthengine authenticate` or a service account): satellite NDVI, CHIRPS, ERA5-Land, SMAP.
- `GEMINI_API_KEY` (or `GEMINI_USE_VERTEX=true`): plant diagnosis. Without it `/diagnosis` returns 503.
- `TRANSLATE_API_KEY`: server-side translation of all dynamic content. Without it content is English.
- `AUTH_MODE=firebase` + `FIREBASE_CREDENTIALS_PATH`: Firebase Auth and FCM push.

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
