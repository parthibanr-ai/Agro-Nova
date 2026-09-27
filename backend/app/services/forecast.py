"""Hyper-personalised advisory engine.

Combines, for one plot and one crop:
  1. observed conditions (satellite/reanalysis vs. historic normal),
  2. the 10-16 day weather forecast at the plot centroid,
  3. the crop's growth stage (from sowing date) and its heat/water sensitivities,
  4. the ENSO state and the country-specific El Nino / La Nina teleconnection.

It is a transparent rule-based model so every advisory can be explained to the farmer. The
`seasonal_outlook` and `features` in the response are the hooks for an ML model (Vertex AI) trained on
the BigQuery climatology tables (see docs/ARCHITECTURE.md).
"""

from datetime import date

from app.providers.base import DailyForecast, ObservedClimate
from app.services import knowledge
from app.services.enso import EnsoState

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def crop_stage(crop_id: str, sowing_date: date | None, today: date | None = None) -> dict | None:
    if sowing_date is None:
        return None
    today = today or date.today()
    das = (today - sowing_date).days
    if das < 0:
        return {"name": "Not yet sown", "days_after_sowing": das, "water_critical": False}
    for name, start, end, critical in knowledge.crop(crop_id)["stages"]:
        if start <= das <= end:
            return {"name": name, "days_after_sowing": das, "water_critical": critical}
    return {"name": "Post-maturity / harvest", "days_after_sowing": das, "water_critical": False}


def _adv(severity: str, kind: str, title: str, detail: str, actions: list[str]) -> dict:
    return {"severity": severity, "type": kind, "title": title, "detail": detail, "actions": actions}


def enso_advisory(enso: EnsoState, country_code: str, crop_id: str) -> dict | None:
    if not enso.available or enso.phase not in ("el_nino", "la_nina"):
        return None
    impact = knowledge.country(country_code)["enso_impact"][enso.phase]
    crop = knowledge.crop(crop_id)
    label = "El Nino" if enso.phase == "el_nino" else "La Nina"
    sens = crop["enso_sensitivity"]
    severity = {"high": "high", "medium": "medium", "low": "low"}[sens]
    if enso.strength == "weak" and severity == "high":
        severity = "medium"
    detail = (
        f"{label} conditions are present ({enso.strength}, ONI {enso.oni:+.1f}, {enso.season}, {enso.trend}). "
        f"{impact['summary']} Your crop ({crop['name']}) has {sens} ENSO sensitivity. "
        f"Most affected season: {impact['season']}. "
        "This is a seasonal tendency, not a day-by-day forecast; local rainfall can still differ."
    )
    return _adv(severity, "enso", f"{label} outlook for {crop['name']}", detail, impact["actions"])


def build_advisories(
    crop_id: str,
    country_code: str,
    stage: dict | None,
    observed: ObservedClimate | None,
    daily: list[DailyForecast],
    enso: EnsoState,
) -> list[dict]:
    crop = knowledge.crop(crop_id)
    out: list[dict] = []

    # --- observed vs. normal ---
    if observed and observed.rain_mm is not None and observed.rain_normal_mm:
        ratio = observed.rain_mm / observed.rain_normal_mm if observed.rain_normal_mm else None
        if ratio is not None and ratio < 0.6:
            out.append(_adv(
                "high" if ratio < 0.4 else "medium", "drought",
                "Rainfall running well below normal",
                f"Last {observed.window_days} days: {observed.rain_mm:.0f} mm vs. normal {observed.rain_normal_mm:.0f} mm "
                f"({ratio * 100:.0f}%).",
                ["Mulch to conserve soil moisture", "Irrigate by soil moisture, prioritising the critical stage", "Delay optional operations like top-dressing"]))
        elif ratio is not None and ratio > 1.5:
            out.append(_adv(
                "medium", "excess_rain", "Rainfall well above normal",
                f"Last {observed.window_days} days: {observed.rain_mm:.0f} mm vs. normal {observed.rain_normal_mm:.0f} mm.",
                ["Open field drains", "Scout for fungal disease", "Postpone fertiliser to avoid leaching"]))
    if observed and observed.ndvi is not None and observed.ndvi_normal:
        if observed.ndvi < 0.8 * observed.ndvi_normal:
            out.append(_adv(
                "medium", "crop_stress", "Vegetation greenness below last year's level",
                f"Satellite NDVI is {observed.ndvi:.2f} vs. {observed.ndvi_normal:.2f} at this time last year; the crop may be stressed by water, nutrients or pests.",
                ["Walk the field and check the low-vigour patches", "Take a plant photo in AgriN for a diagnosis", "Check soil moisture and soil test"]))
    if observed and observed.soil_moisture_pct is not None and observed.soil_moisture_pct < 12:
        out.append(_adv("medium", "dry_soil", "Soil surface is dry",
                        f"SMAP surface soil moisture is about {observed.soil_moisture_pct:.0f}%.",
                        ["Plan an irrigation if no rain in the next 3 days"]))

    # --- forecast ---
    if daily:
        week = daily[:7]
        rain_7d = sum(d.rain_mm for d in week)
        max_t = max(d.tmax_c for d in week)
        min_t = min(d.tmin_c for d in week)
        heavy = [d for d in week if d.rain_mm >= 50]
        critical = bool(stage and stage["water_critical"])

        if heavy:
            out.append(_adv("high", "heavy_rain", "Heavy rain expected",
                            f"About {heavy[0].rain_mm:.0f} mm forecast on {heavy[0].day.isoformat()}.",
                            ["Do not irrigate or spray before the rain", "Clear drains; harvest mature produce early if possible", "Secure stored grain"]))
        if max_t >= crop["heat_stress_c"]:
            out.append(_adv("high" if critical else "medium", "heat", f"Heat stress risk for {crop['name']}",
                            f"Forecast maximum {max_t:.0f} C reaches the crop's stress threshold ({crop['heat_stress_c']} C)"
                            + (f" during {stage['name']}." if critical else "."),
                            ["Irrigate in the evening/early morning", "Mulch and avoid spraying in midday heat", "Foliar 2% potassium/kaolin where advised"]))
        if crop["frost_sensitive"] and min_t <= 2:
            out.append(_adv("high", "frost", "Frost risk", f"Forecast minimum {min_t:.0f} C.",
                            ["Light irrigation the evening before", "Cover nursery/young plants", "Smoke/straw fires upwind on cold nights"]))
        if rain_7d < 5 and (critical or max_t >= crop["heat_stress_c"] - 3):
            out.append(_adv("medium", "irrigation", "Dry week ahead - plan irrigation",
                            f"Only {rain_7d:.0f} mm rain forecast in 7 days"
                            + (f" while the crop is in a water-critical stage ({stage['name']})." if critical else "."),
                            ["Irrigate at the critical stage", "Use drip/alternate wetting-drying to save water"]))
        elif rain_7d >= 25 and not heavy:
            out.append(_adv("info", "skip_irrigation", "Useful rain forecast - you can skip irrigation",
                            f"About {rain_7d:.0f} mm rain forecast in the next 7 days.",
                            ["Skip the next scheduled irrigation", "Delay fertiliser until after the rain"]))

    # --- ENSO ---
    e = enso_advisory(enso, country_code, crop_id)
    if e:
        out.append(e)

    out.sort(key=lambda a: SEVERITY_ORDER[a["severity"]])
    return out


def build_forecast_report(*, plot, observed, daily, enso, today: date | None = None) -> dict:
    stage = crop_stage(plot.crop, plot.sowing_date, today)
    advisories = build_advisories(plot.crop, plot.country, stage, observed, daily, enso)
    return {
        "plot_id": plot.id,
        "crop": plot.crop,
        "growth_stage": stage,
        "observed": None if observed is None else {
            "window_days": observed.window_days,
            "rain_mm": observed.rain_mm,
            "rain_normal_mm": observed.rain_normal_mm,
            "tmean_c": observed.tmean_c,
            "ndvi": observed.ndvi,
            "ndvi_normal": observed.ndvi_normal,
            "soil_moisture_pct": observed.soil_moisture_pct,
            "sources": observed.sources,
        },
        "daily": [
            {"date": d.day.isoformat(), "tmax_c": d.tmax_c, "tmin_c": d.tmin_c, "rain_mm": d.rain_mm, "et0_mm": d.et0_mm}
            for d in daily
        ],
        "enso": enso.to_dict(),
        "advisories": advisories,
        "disclaimer": "Forecasts are probabilistic. Use them with your own field observations and local extension advice.",
    }
