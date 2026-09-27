"""Fill the local database with demo farmers, plots, soil samples and livestock.

Run from the backend folder (so it uses the same agrin.db as the server):

    cd D:\\AgriN\\backend
    .venv\\Scripts\\python scripts\\seed_sample_data.py           # add demo data (safe to repeat)
    .venv\\Scripts\\python scripts\\seed_sample_data.py --reset   # delete the demo users' data first

All names, coordinates and soil values are made up for demonstration: plot locations are real farming
regions but the boundaries are synthetic rectangles, not real farms. Every plot goes through the same
validation as the API.
"""

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.domain.polygon import centroid, polygon_area_m2, validate_plot  # noqa: E402
from app.models import Plot, SoilSample, User  # noqa: E402
from app.services import knowledge  # noqa: E402

TODAY = date.today()


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


def rectangle(lat: float, lon: float, width_m: float, height_m: float) -> list[dict]:
    """Four corners (SW, SE, NE, NW: walking order) of a rectangle centred on lat/lon."""
    dlat = height_m / 2 / 111_320
    dlon = width_m / 2 / (111_320 * math.cos(math.radians(lat)))
    pts = [(lat - dlat, lon - dlon), (lat - dlat, lon + dlon), (lat + dlat, lon + dlon), (lat + dlat, lon - dlon)]
    return [{"lat": round(a, 6), "lon": round(b, 6)} for a, b in pts]


# uid -> profile + plots. `soil` entries are (offset_lat_m, offset_lon_m, source, days_ago, values).
DEMO = {
    "dev-farmer": {  # the account the app signs in with by default
        "language": "hi", "country": "IN", "state": "Maharashtra", "livestock": {"cows": 2},
        "fcm_token": "sample-fcm-token-dev-farmer",
        "plots": [
            {"name": "Nashik tomato field", "state": "Maharashtra", "crop": "tomato", "at": (19.9975, 73.7898),
             "size": (110, 100), "sown": days_ago(55),
             "soil": [(10, -15, "soil_health_card", 90, {"ph": 7.9, "oc": 0.42, "n": 210, "p": 14, "k": 260, "ec": 0.35}),
                      (-20, 25, "soil_health_card", 90, {"ph": 8.1, "oc": 0.38, "n": 198, "p": 12, "k": 240, "ec": 0.41})]},
            {"name": "Ludhiana wheat plot", "state": "Punjab", "crop": "wheat", "at": (30.9010, 75.8573),
             "size": (200, 150), "sown": days_ago(70),
             "soil": [(0, 0, "lab_test", 30, {"ph": 7.6, "oc": 0.61, "n": 285, "p": 22, "k": 190})]},
            {"name": "Anantapur millet & groundnut", "state": "Andhra Pradesh", "crop": "millet", "at": (14.6819, 77.6006),
             "size": (140, 90), "sown": days_ago(35), "soil": []},  # no sample: shows the "enter soil data" prompt
        ],
    },
    "farmer-in-south": {
        "language": "ta", "country": "IN", "state": "Tamil Nadu", "livestock": {},
        "plots": [
            {"name": "Thanjavur paddy", "state": "Tamil Nadu", "crop": "rice", "at": (10.7870, 79.1378),
             "size": (160, 120), "sown": days_ago(72),
             "soil": [(5, 5, "field_kit", 20, {"ph": 6.4, "oc": 0.71})]},
        ],
    },
    "farmer-br": {
        "language": "pt-BR", "country": "BR", "state": "Mato Grosso", "livestock": {},
        "plots": [
            {"name": "Sorriso soja", "state": "Mato Grosso", "crop": "soybean", "at": (-12.5450, -55.7110),
             "size": (400, 300), "sown": days_ago(60),
             "soil": [(0, 0, "lab_test", 45, {"ph": 5.3, "oc": 1.8, "p": 9, "k": 85})]},
            {"name": "Varginha café", "state": "Minas Gerais", "crop": "coffee", "at": (-21.5510, -45.4300),
             "size": (180, 140), "sown": days_ago(200), "soil": []},
        ],
    },
    "farmer-ru": {
        "language": "ru", "country": "RU", "state": "Krasnodar Krai", "livestock": {},
        "plots": [
            {"name": "Краснодар пшеница", "state": "Krasnodar Krai", "crop": "wheat", "at": (45.0448, 38.9760),
             "size": (500, 350), "sown": days_ago(150),
             "soil": [(0, 0, "lab_test", 60, {"ph": 6.9, "oc": 3.2, "p": 24, "k": 175})]},
        ],
    },
    "farmer-cn": {
        "language": "zh-CN", "country": "CN", "state": "Heilongjiang", "livestock": {},
        "plots": [
            {"name": "哈尔滨水稻田", "state": "Heilongjiang", "crop": "rice", "at": (45.8038, 126.5350),
             "size": (300, 200), "sown": days_ago(85),
             "soil": [(0, 0, "lab_test", 25, {"ph": 6.2, "oc": 2.4, "n": 320})]},
            {"name": "郑州小麦地", "state": "Henan", "crop": "wheat", "at": (34.7466, 113.6253),
             "size": (250, 180), "sown": days_ago(110), "soil": []},
        ],
    },
}


def offset(lat: float, lon: float, dy_m: float, dx_m: float) -> tuple[float, float]:
    return lat + dy_m / 111_320, lon + dx_m / (111_320 * math.cos(math.radians(lat)))


def seed(reset: bool) -> None:
    init_db()
    db = SessionLocal()
    try:
        if reset:
            for uid in DEMO:
                user = db.get(User, uid)
                if user:
                    db.delete(user)  # cascades to plots and soil samples
            db.commit()
            print("Removed existing demo users and their data.")

        for uid, prof in DEMO.items():
            user = db.get(User, uid)
            if user is None:
                user = User(uid=uid)
                db.add(user)
            user.language, user.country, user.state = prof["language"], prof["country"], prof["state"]
            user.livestock = prof["livestock"]
            user.fcm_token = prof.get("fcm_token")
            db.commit()

            existing = {p.name for p in db.query(Plot).filter(Plot.owner_uid == uid)}
            for spec in prof["plots"]:
                if spec["name"] in existing:
                    continue
                knowledge.crop(spec["crop"])  # fail loudly on a typo
                lat, lon = spec["at"]
                corners = rectangle(lat, lon, *spec["size"])
                pts = [(c["lat"], c["lon"]) for c in corners]
                validate_plot(pts, tuple(knowledge.country(prof["country"])["bbox"]))
                clat, clon = centroid(pts)
                plot = Plot(owner_uid=uid, name=spec["name"], country=prof["country"], state=spec["state"],
                            crop=spec["crop"], sowing_date=spec["sown"], corners=corners,
                            area_m2=polygon_area_m2(pts), centroid_lat=clat, centroid_lon=clon)
                db.add(plot)
                db.flush()
                for dy, dx, source, ago, values in spec["soil"]:
                    slat, slon = offset(clat, clon, dy, dx)
                    db.add(SoilSample(plot_id=plot.id, lat=round(slat, 6), lon=round(slon, 6), source=source,
                                      sampled_on=days_ago(ago), values=values))
                db.commit()
                print(f"  {uid:16} + {spec['name']} ({spec['crop']}, {plot.area_m2 / 10_000:.2f} ha, "
                      f"{len(spec['soil'])} soil sample(s))")
        print("\nDone. The app signs in as 'dev-farmer' by default.")
        print("Try another farmer: flutter run --dart-define=DEV_USER=farmer-br")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete the demo users' data before seeding")
    seed(ap.parse_args().reset)
