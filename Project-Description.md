# Agro Nova: Regenerative Agricultural Intelligence

**Agro Nova** is a regenerative-agriculture assistant for smallholder farmers in India. It is built to extend to Brazil, Russia and China, and the same Flutter code runs on Android, iOS and the web.

1. **Plot capture:** a farmer marks four field corners by tapping the map, using GPS, typing coordinates or searching a place. The backend validates the polygon (shape, area, country) with Shapely and pyproj.
2. **Soil:** it shows public soil data from ISRIC SoilGrids, lets the farmer enter government or lab sample values with a GPS point, and explains how to get soil tested locally.
3. **Weather and climate:** a crop-specific outlook combines a 10-day forecast with satellite NDVI, rainfall against normal and soil moisture from Google Earth Engine (CHIRPS, ERA5-Land, SMAP). It falls back to Open-Meteo, and includes El Niño / La Niña effects per country.
4. **Plant diagnosis:** a leaf photo goes to Gemini, which identifies the likely deficiency, pest or disease. The app returns an organic remedy with preparation steps and explains why organic beats chemical.
5. **What to grow next:** Gemini recommends crops for the next local sowing season (Kharif, Rabi, Zaid and so on), grounded in the plot's soil, satellite data, ENSO state and location.
6. **Resilience, water saving and value addition:** each has a generic guide (compost, biogas, milk, water tips, market ideas) plus a Gemini section personalised to the plot, crop and livestock. The app also has a cow calculator.
7. **Schemes and alerts:** a government schemes and subsidies catalogue, with scheme, weather and market push notifications sent through Firebase Cloud Messaging.
8. **Languages:** 22 Indian languages plus Portuguese, Russian and Chinese. Screen labels are built into the app, and dynamic advice is translated by the Cloud Translation API and cached.
9. **Backend stack:** Python 3.12, FastAPI, Pydantic and SQLAlchemy (SQLite locally, Postgres for production). It uses the `google-genai` SDK with retry and model fallback, `earthengine-api` and `firebase-admin`. It is tested with pytest (45 tests) and designed for Cloud Run.
10. **App stack, auth and data:** Flutter with Provider, `google_maps_flutter`, geolocator, image_picker and Firebase Auth and Messaging. The backend supports a no-credentials dev login or Firebase login, and every Google service is optional and fails soft. An Earth Engine to BigQuery pipeline for climatology (future Vertex AI seasonal model) is included.
