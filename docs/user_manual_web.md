# Agro Nova web manual

How to run and use the Agro Nova web app (runs in Chrome or any modern browser). For Android see [user_manual_mobile.md](user_manual_mobile.md). Backend settings are in [env_reference.md](env_reference.md).

## 1. What runs where

| Part | Folder | Runs as |
|---|---|---|
| Backend API (FastAPI, Python) | `backend/` | `uvicorn`, port 8000 |
| Web app (Flutter web) | `app/` | A Flutter dev server that opens Chrome |

The browser calls the backend directly, so **start the backend first**, and the backend must allow the web app's address (CORS, section 3).

## 2. One-time setup

Needs Python 3.12, Flutter (`D:\dev\flutter`) and Google Chrome. Run `flutter doctor`; **Flutter** and **Chrome** must show a green tick. Android Studio is **not** needed for web.

```powershell
cd D:\AgriN\backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env

cd D:\AgriN\app
flutter pub get
flutter gen-l10n
```

If `flutter` is not recognised, close and reopen VS Code, or run `$env:Path += ";D:\dev\flutter\bin"`.

## 3. Run in Chrome

**Terminal 1: backend**

```powershell
cd D:\AgriN\backend
.venv\Scripts\uvicorn app.main:app --reload
```

**Terminal 2: web app**

```powershell
cd D:\AgriN\app
flutter run -d chrome --web-port 8080 --dart-define=API_BASE_URL=http://localhost:8000
```

**Use `--web-port 8080`.** Without it Flutter picks a random port, the browser blocks the API calls (CORS error), and the app shows "Could not load". The backend allows `http://localhost:8080` and `http://localhost:5173` by default (`CORS_ORIGINS` in `.env`). If you use another port, add it there and restart the backend.

While running, **r** hot-reloads, **R** restarts, **q** quits. To try it on a phone browser on the same Wi-Fi, run with `-d web-server --web-hostname 0.0.0.0 --web-port 8080`, add `http://<your-pc-ip>:8080` to `CORS_ORIGINS`, set `API_BASE_URL` to `http://<your-pc-ip>:8000`, and start uvicorn with `--host 0.0.0.0`.

## 4. Configuration for the web app

| Item | How | Needed for |
|---|---|---|
| Google Maps key | In `app/web/index.html`, inside `<head>`, add `<script src="https://maps.googleapis.com/maps/api/js?key=YOUR_KEY"></script>`. Create the key in Google Cloud Console; enable **Maps JavaScript API**. Restrict it to your site's address (HTTP referrer). | The plot-capture map. Without it the map area is blank. |
| `API_BASE_URL` | `--dart-define=API_BASE_URL=...` | Pointing at a different backend (defaults to `http://localhost:8000`) |
| Firebase | Run `flutterfire configure` in `app/` and select the web platform | Login. Without it the app uses `AUTH_MODE=dev` login. **Push notifications on web are not set up** (they need a web service worker and a VAPID key). |
| CORS | `CORS_ORIGINS` in `backend/.env` | Letting the browser call the API |

## 5. Browser behaviour to know about

- **GPS:** the browser asks for location permission. It works on `localhost` and on `https://` sites, not on plain `http://` to another address. On a desktop the location is approximate (Wi-Fi/IP based), so for real field corners tap the map or use a phone.
- **Photos:** **Take a photo** opens the device camera on a phone browser and a file picker on a desktop. Choose a clear JPEG or PNG under 8 MB.
- **Privacy and my data:** the three-dot menu (top right) lets you change what you allow, download everything we hold about you, or delete it permanently. The app asks what you allow the first time you open it.
- **Delete my data:** in the same menu; permanently removes your plots, soil records and profile from the server.
- **Language:** the translate icon (top right). Fixed labels are translated in the browser; advice and schemes come from the backend (needs `TRANSLATE_API_KEY`).
- **Data:** plots and soil samples are stored by the backend, not in the browser, so clearing browser data does not delete them. The selected language is remembered in the browser.

## 6. Using the app

1. **Language:** tap the translate icon and pick a language.
2. **Add a plot:** tap **Add a plot**. Find the field by typing a place name in the **Search a place** box above the map (uses OpenStreetMap, with Google Geocoding as a fallback when `GOOGLE_MAPS_API_KEY` is set) or by scrolling/zooming manually. Mark all four corners: click the map at each corner (in walking order), or use the pin-with-pencil button to type a latitude/longitude directly. Enter name, crop, country and sowing date, then **Save plot**. If the shape is invalid the message says what to fix.
3. **Soil:** modelled public soil data plus how to get your soil tested locally. **Add soil sample** enters values from a government or lab report with the sample's latitude and longitude.
4. **Weather & climate outlook:** 10-day forecast, recent rainfall against normal, and crop-specific advice including El Nino / La Nina effects.
5. **Diagnose plant:** upload a leaf photo to get the likely deficiency, pest or disease, an organic remedy with preparation steps, and why organic beats chemical. Needs `GEMINI_API_KEY`.
6. **Government schemes, Self-resilient farming, Save water, Value addition & market:** open from the home screen.

Advice is a guide, not a certified diagnosis.

## 7. Building and deploying the web app

```powershell
cd D:\AgriN\app
flutter build web --dart-define=API_BASE_URL=https://your-backend.example.com
```

The output is `app\build\web` (static files). Host it anywhere that serves static files, for example Firebase Hosting (`firebase init hosting`, public directory `build/web`, single-page app: yes, then `firebase deploy`). For a deployed site:

- Serve the backend over **https** (Cloud Run does this), and add the site's address to `CORS_ORIGINS`.
- Set `AUTH_MODE=firebase` so users must sign in; `dev` mode is for local use only.
- Restrict the Maps and Translation keys to your site.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| App shows "Could not load" and the browser console (F12) shows a **CORS** error | Run with `--web-port 8080`, or add your port's address to `CORS_ORIGINS` and restart the backend |
| "Could not load" with no CORS error | Backend not running or wrong `API_BASE_URL`. Test http://localhost:8000/api/v1/health |
| Map area is blank | Add the Maps JavaScript script to `index.html` (section 4) |
| GPS button says permission denied | Allow location for the site in the browser's address-bar padlock menu; use `localhost` or `https` |
| `flutter` is not recognised | Restart VS Code, or `$env:Path += ";D:\dev\flutter\bin"` |
| Old version still showing after a build | Hard refresh with Ctrl+Shift+R (the app caches itself) |
| Diagnosis returns 503 | Set `GEMINI_API_KEY` in `backend/.env` and restart the backend |
| Advice not translated | Set `TRANSLATE_API_KEY` |
| First forecast is slow | Normal when Earth Engine is on (10 to 30 seconds); later requests are faster |

## 9. Tests

```powershell
cd D:\AgriN\backend; .venv\Scripts\python -m pytest     # backend, 45 tests
cd D:\AgriN\app;     flutter analyze; flutter test       # app
```
