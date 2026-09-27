# Agro Nova mobile manual (Android)

How to run and use the Agro Nova Android app. For the web version see [user_manual_web.md](user_manual_web.md). Backend settings are in [env_reference.md](env_reference.md).

## 1. What runs where

| Part | Folder | Runs as |
|---|---|---|
| Backend API (FastAPI, Python) | `backend/` | `uvicorn` on your PC, port 8000 |
| Mobile app (Flutter) | `app/` | Android emulator or a real phone |

The app calls the backend over HTTP, so **start the backend first**.

## 2. One-time setup

Already installed on this PC: Python 3.12, Flutter (`D:\dev\flutter`), Android Studio, the Android SDK and the `Pixel_10` emulator. On another PC, install the same, then run `flutter doctor` until **Flutter, Android toolchain** and **Connected device** show a green tick (the Visual Studio warning only affects Windows desktop apps; ignore it). Accept Android licences once: `flutter doctor --android-licenses`.

```powershell
cd D:\AgriN\backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env

cd D:\AgriN\app
flutter pub get
flutter gen-l10n
```

If `flutter` is not recognised, close and reopen VS Code (it reads PATH only at start-up), or run `$env:Path += ";D:\dev\flutter\bin"`.

Keep this line in `app/android/gradle.properties`: `kotlin.incremental=false`. Without it, the Android build fails because the project is on `D:` while Flutter's package cache is on `C:`.

## 3. Run on the emulator

**Terminal 1: backend**

```powershell
cd D:\AgriN\backend
.venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0
```

Check http://localhost:8000/api/v1/health in a browser. `--host 0.0.0.0` lets the emulator reach it.

**Terminal 2: emulator and app**

```powershell
flutter emulators --launch Pixel_10
```

The Pixel_10 window always opens off-screen (taller than the display) — with it running, run this from `D:\AgriN` to move it into view and zoom out so the whole phone fits:
```powershell
.\tools\fit_emulator.ps1
```
Or click the phone and press **Ctrl+Down** to zoom out (Ctrl+Up zooms in). This has to be done again every time the emulator is relaunched; it doesn't persist.

Set the Google Maps key for this session before running (see section 6) - without it the plot-capture
map is blank or crashes with "API key not found":
```powershell
$env:MAPS_API_KEY="AIzaSy..."
cd D:\AgriN\app
flutter run
```

Pick `emulator-5554` if asked. The first build takes 5 to 10 minutes (Gradle downloads dependencies once); later runs take about a minute. While running, **r** hot-reloads, **R** restarts, **q** quits.

The emulator reaches your PC at `http://10.0.2.2:8000`, which the app uses by default.

## 4. Run on a real phone

1. On the phone: Settings > About phone > tap **Build number** 7 times, then Settings > System > Developer options > turn on **USB debugging**.
2. Connect by USB and accept the prompt on the phone. `flutter devices` should list it.
3. Put the phone on the same Wi-Fi as the PC. Find the PC's address with `ipconfig` (the IPv4 address, e.g. `192.168.1.20`).
4. Allow port 8000 through Windows Firewall when prompted (or add an inbound rule).
5. Run:
   ```powershell
   cd D:\AgriN\app
   flutter run --dart-define=API_BASE_URL=http://192.168.1.20:8000
   ```

Debug builds allow plain `http://` to your PC (the debug manifest sets `usesCleartextTraffic`). **Release builds must use an `https://` backend.**

## 5. Permissions the app asks for

| Permission | Used for |
|---|---|
| Location | "Use my GPS" when capturing plot corners and soil-sample spots |
| Camera | Taking a plant photo for diagnosis |
| Notifications | Scheme, weather and market alerts (needs Firebase, see below) |

If you deny one, use the corresponding alternative: tap the map instead of GPS, or pick a photo from the gallery.

## 6. Configuration for the mobile app

| Item | How | Needed for |
|---|---|---|
| Google Maps key | Get a key from Google Cloud Console > APIs & Services with **Maps SDK for Android** enabled, restricted to this app's package name (`in.agrin.agrin`) and SHA-1 fingerprint. Set it as the `MAPS_API_KEY` environment variable in the shell you run `flutter run`/`flutter build` from (e.g. `$env:MAPS_API_KEY="AIzaSy..."` in PowerShell); `android/app/build.gradle.kts` injects it into the manifest at build time, so the key itself never lives in source control. | The plot-capture map. Without a working key the map area is blank or crashes with "API key not found". |
| Firebase | Install the FlutterFire CLI and run `flutterfire configure` in `app/`; this adds `google-services.json`. | Login and push notifications. Without it the app uses `AUTH_MODE=dev` login. |
| `API_BASE_URL` | `--dart-define=API_BASE_URL=...` on `flutter run` | Pointing at a different backend |
| Language labels | Hand-written for English, Hindi, Tamil, Portuguese, Russian, Chinese. For the rest set `TRANSLATE_API_KEY` in your shell, run `python tools/translate_arb.py`, then `flutter gen-l10n`. Have a native speaker review. | Fixed screen labels in other Indian languages |

## 7. Using the app

1. **Language:** tap the translate icon (top right) and pick a language.
2. **Add a plot:** tap **Add a plot**. Find the field either by typing a place name in the **Search a place** box above the map (results come from OpenStreetMap, no key needed) or by scrolling/zooming manually. Then mark all four corners: tap the map at each corner (in walking order), stand at each corner and press the **GPS** button, or press the pin-with-pencil button to type a latitude/longitude directly. Enter name, crop, country and sowing date, then **Save plot**. If the shape is invalid (crossing lines, wrong country) the message says what to fix.
3. **Soil:** shows modelled public soil data and how to get your soil tested locally. Tap **Add soil sample** to enter values from a government or lab report; press GPS at the sampling spot to record its latitude and longitude.
4. **Weather & climate outlook:** 10-day forecast, recent rainfall against normal, and advice specific to your crop and growth stage, including El Nino / La Nina effects.
5. **Diagnose plant:** take a photo of the affected leaf (close, in daylight) or choose one from the gallery. You get the likely deficiency, pest or disease, a natural/organic remedy with preparation steps, and why organic beats chemical. Needs `GEMINI_API_KEY` on the backend.
6. **Government schemes, Self-resilient farming (cow calculator), Save water, Value addition & market:** open from the home screen.

Advice is a guide, not a certified diagnosis. For serious problems consult your local Krishi Vigyan Kendra or agriculture officer.

## 8. Building an installable APK

```powershell
cd D:\AgriN\app
flutter build apk --release --dart-define=API_BASE_URL=https://your-backend.example.com
```

The file is `app\build\app\outputs\flutter-apk\app-release.apk`. For the Play Store, sign the app and build an app bundle (`flutter build appbundle`); see https://docs.flutter.dev/deployment/android. iOS builds need a Mac.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `flutter` is not recognised | Restart VS Code, or `$env:Path += ";D:\dev\flutter\bin"` |
| Build error mentioning "different roots" (`D:\` vs `C:\`) | Make sure `kotlin.incremental=false` is in `app/android/gradle.properties`, then `flutter clean` and rebuild |
| App shows "Could not load" | Backend not running, or wrong address. Test http://localhost:8000/api/v1/health on the PC. On a real phone use your PC's IP, not `10.0.2.2`, and allow port 8000 in the firewall. |
| App works but every request fails with a network error | Cleartext HTTP is only allowed in debug builds; a release build needs an `https://` backend |
| Emulator window off-screen or taller than the screen | Run `.\tools\fit_emulator.ps1`, or Ctrl+Down on the phone, or launch with `-scale 0.3` |
| Emulator shows only the wallpaper | The app is not running yet. Check the `flutter run` terminal, or swipe up on the phone and open **Agro Nova** |
| Map is blank | Add the Google Maps key (section 6) |
| GPS button does nothing on the emulator | Set a location: emulator side panel **...** > Location > Set location |
| Diagnosis returns 503 | Set `GEMINI_API_KEY` in `backend/.env` and restart the backend |
| Advice not translated | Set `TRANSLATE_API_KEY` |
| Licences not accepted | `flutter doctor --android-licenses`, answer `y` |

## 10. Tests

```powershell
cd D:\AgriN\backend; .venv\Scripts\python -m pytest     # backend, 33 tests
cd D:\AgriN\app;     flutter analyze; flutter test       # app
```
