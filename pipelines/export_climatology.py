"""Big-data path: export country-wide climatology from Earth Engine into BigQuery.

The API computes per-plot anomalies live from Earth Engine. For country/regional analytics and for
training the seasonal-outlook model in Vertex AI, we materialise gridded monthly climatology once
into BigQuery (which then scales to all BRIC countries with SQL / Dataflow / BigQuery ML).

Usage:
    python export_climatology.py --project my-gcp --dataset agrin --country IN --start 2000 --end 2025

Requires: earthengine-api, an authenticated EE account registered for a Cloud project, and a BigQuery dataset.
"""

import argparse

import ee

# Country bounding boxes (min_lon, min_lat, max_lon, max_lat); keep in sync with backend/app/data/countries.json
BBOX = {
    "IN": (68.0, 6.0, 97.5, 37.5),
    "BR": (-74.5, -34.0, -34.0, 5.5),
    "RU": (19.0, 41.0, 180.0, 82.0),
    "CN": (73.0, 17.5, 135.5, 54.0),
}


def monthly_rain(region: ee.Geometry, year: int, month: int) -> ee.Image:
    start = ee.Date.fromYMD(year, month, 1)
    return (
        ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        .filterDate(start, start.advance(1, "month"))
        .select("precipitation")
        .sum()
        .rename("rain_mm")
        .set({"year": year, "month": month})
    )


def build_table(country: str, start_year: int, end_year: int, scale_m: int = 25_000) -> ee.FeatureCollection:
    region = ee.Geometry.Rectangle(BBOX[country])
    feats = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            img = monthly_rain(region, year, month)
            samples = img.sample(region=region, scale=scale_m, geometries=True)
            feats.append(samples.map(lambda f, y=year, m=month: f.set({"country": country, "year": y, "month": m})))
    return ee.FeatureCollection(feats).flatten()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", default="agrin")
    p.add_argument("--country", choices=sorted(BBOX), required=True)
    p.add_argument("--start", type=int, default=2000)
    p.add_argument("--end", type=int, default=2025)
    a = p.parse_args()

    ee.Initialize(project=a.project)
    table = build_table(a.country, a.start, a.end)
    task = ee.batch.Export.table.toBigQuery(
        collection=table,
        table=f"{a.project}.{a.dataset}.rain_monthly_{a.country.lower()}",
        description=f"agrin_rain_monthly_{a.country}",
        append=False,
    )
    task.start()
    print(f"Started EE export task {task.id} -> BigQuery {a.project}.{a.dataset}.rain_monthly_{a.country.lower()}")


if __name__ == "__main__":
    main()
