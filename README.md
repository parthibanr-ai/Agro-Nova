# AgriN - Regenerative Agricultural Intelligence

Web + mobile app (one Flutter codebase) with a FastAPI backend on Google Cloud. For farmers in India, and
extensible to Brazil, Russia and China (BRIC).

| Requirement | Where it lives |
|---|---|
| Four-corner plot capture (map tap or GPS), server validation | `app/lib/screens/plot_capture_screen.dart`, `backend/app/domain/polygon.py` |
| Soil data from public sources; prompt to enter existing govt data or how to get it tested; sample lat/lon | `backend/app/services/soil.py`, `app/lib/screens/soil_screen.dart` |
| Satellite-driven, crop-personalised weather outlook (GEE, extensible) | `backend/app/providers/`, `backend/app/services/forecast.py` |
| El Nino / La Nina in forecasting | `backend/app/services/enso.py` + country teleconnections in `data/countries.json` |
| Plant photo -> deficiency/pest/disease -> organic remedy + why organic beats chemical | `backend/app/services/diagnosis.py` (Gemini), `data/remedies.json` |
| Government schemes/subsidies + push | `data/schemes.json`, `services/notifications.py` (FCM) |
| Self-resilient farming (cow dung -> compost, milk income, biogas) | `services/advice.py`, `data/resilience.json` |
| Water and resource saving tips | `data/water_tips.json` |
| Value addition + market access | `data/value_add.json` |
| 22 Indian languages + Portuguese, Russian, Chinese | `backend/app/core/languages.py`, `app/lib/l10n`, `services/translation.py` |
| Big data | `pipelines/export_climatology.py` (Earth Engine -> BigQuery) |

Design and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Run the backend

```powershell
cd backend
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
copy .env.example .env         # AUTH_MODE=dev works without any Google credentials
.venv\Scripts\python -m pytest # 32 tests
.venv\Scripts\uvicorn app.main:app --reload   # http://localhost:8000/docs (send header X-Dev-User: me)
```

Without credentials the API still works: weather comes from Open-Meteo, soil from ISRIC SoilGrids. Add
credentials to unlock the Google services:

- `GEE_CLOUD_PROJECT` (+ `earthengine authenticate` or a service account): satellite NDVI, CHIRPS, ERA5-Land, SMAP.
- `GEMINI_API_KEY` (or `GEMINI_USE_VERTEX=true`): plant diagnosis. Without it `/diagnosis` returns 503.
- `TRANSLATE_API_KEY`: server-side translation of all dynamic content. Without it content is English.
- `AUTH_MODE=firebase` + `FIREBASE_CREDENTIALS_PATH`: Firebase Auth and FCM push.

## Run the app

Flutter is not installed on the machine this was written on, so **the Flutter code has not been compiled
or run**. Expect to fix small compile errors on first build.

```powershell
cd app
flutter create . --platforms=android,ios,web   # generates platform folders (keeps lib/)
flutter pub get
flutter gen-l10n
flutter run -d chrome --dart-define=API_BASE_URL=http://localhost:8000
```

Add a Google Maps API key (Android manifest, iOS AppDelegate, web `index.html`) and run
`flutterfire configure` for Firebase. Translate the remaining UI languages with
`python tools/translate_arb.py` (needs `TRANSLATE_API_KEY`).
