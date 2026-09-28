# Agro Nova user manual

The manual is split by platform. Pick the one you need.

| Manual | For |
|---|---|
| [Mobile manual](user_manual_mobile.md) | Android emulator or phone: setup, running, permissions, APK build, troubleshooting |
| [Web manual](user_manual_web.md) | Chrome or any browser: setup, running, CORS, Maps key, deployment, troubleshooting |
| [Backend settings reference](env_reference.md) | Every value in `backend/.env` (login, Earth Engine, Gemini, Translation), shared by both |
| [Operations guide](OPERATIONS.md) | Running it in production: metrics, alerts, Redis, queues, privacy (consent, retention), offline behaviour |
| [Local monitoring](../ops/monitoring/README.md) | Prometheus + Grafana dashboard on your PC (`docker compose up -d`) |
| [Architecture](ARCHITECTURE.md) | Design, data sources and roadmap |

## Demo data

Fill an empty database with sample farmers, plots, soil samples and livestock (India, Brazil, Russia, China):

```powershell
cd D:\AgriN\backend
.venv\Scripts\python scripts\seed_sample_data.py           # add demo data (safe to repeat)
.venv\Scripts\python scripts\seed_sample_data.py --reset   # wipe the demo users' data first
```

The app signs in as `dev-farmer` by default (3 Indian plots, 2 cows, Hindi). To see another demo farmer, run the app with `--dart-define=DEV_USER=<name>`:

| Login (`DEV_USER`) | Country and language | Plots |
|---|---|---|
| `dev-farmer` (default) | India, Hindi | Tomato (Nashik), wheat (Ludhiana), millet (Anantapur, no soil sample) |
| `farmer-in-south` | India, Tamil | Rice (Thanjavur) |
| `farmer-br` | Brazil, Portuguese | Soybean (Sorriso), coffee (Varginha) |
| `farmer-ru` | Russia, Russian | Wheat (Krasnodar) |
| `farmer-cn` | China, Chinese | Rice (Harbin), wheat (Zhengzhou) |

The first time each demo farmer opens the app, the privacy screen appears and asks what to allow; the choice is stored with that farmer in `backend/agrin.db`, so it is asked once (again if the notice version changes). All values are made up for demonstration; the plots are synthetic rectangles in real farming regions, not real farms. The data lives in `backend/agrin.db`, so restart nothing: refresh the app (press **r**, or pull the home screen) after seeding.

Both apps talk to the same backend (`backend/`), so start the backend first:

```powershell
cd D:\AgriN\backend
.venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0
```
