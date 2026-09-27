"""Schemes, resilience (livestock), water-saving tips and value-addition / market advice."""

from app.services import knowledge
from app.services.enso import EnsoState


# ---------------------------------------------------------------- schemes
def matching_schemes(country: str, state: str | None, crop: str | None, area_ha: float | None,
                     category: str | None = None) -> list[dict]:
    data = knowledge.load("schemes")
    out = []
    for s in data["schemes"]:
        if s["country"] != country.upper():
            continue
        if category and s["category"] != category:
            continue
        if s["states"] and state and state not in s["states"]:
            continue
        if s["crops"] and crop and crop not in s["crops"]:
            continue
        if s["max_area_ha"] is not None and area_ha is not None and area_ha > s["max_area_ha"]:
            continue
        out.append(s)
    return out


def schemes_response(country: str, state: str | None, crop: str | None, area_ha: float | None,
                     category: str | None = None) -> dict:
    return {
        "schemes": matching_schemes(country, state, crop, area_ha, category),
        "note": knowledge.load("schemes")["verification_note"],
    }


# ---------------------------------------------------------------- resilience
def livestock_estimate(cows: int, area_acres: float | None = None, **overrides: float) -> dict:
    """Indicative annual economics + manure self-sufficiency of keeping `cows` milch animals."""
    res = knowledge.load("resilience")
    d = {**res["defaults"], **{k: v for k, v in overrides.items() if v is not None}}
    milk_l_year = cows * d["milk_l_per_cow_per_day"] * d["lactation_days_per_year"]
    milk_income = milk_l_year * d["milk_price_per_l"]
    feed_cost = cows * d["feed_cost_per_cow_per_day"] * 365
    dung_t_year = cows * d["dung_kg_per_cow_per_day"] * 365 / 1000
    compost_t_year = dung_t_year * d["compost_yield_fraction"]
    manure_value_note = "Manure replaces bought fertiliser; its value is not counted in the milk margin below."
    acres_fed = compost_t_year / d["manure_t_per_acre_per_year"]
    biogas_m3_day = cows * d["dung_kg_per_cow_per_day"] * d["biogas_m3_per_kg_dung"]
    out = {
        "inputs": {"cows": cows, **d},
        "milk_litres_per_year": round(milk_l_year),
        "milk_income_per_year": round(milk_income),
        "feed_cost_per_year": round(feed_cost),
        "net_milk_margin_per_year": round(milk_income - feed_cost),
        "dung_tonnes_per_year": round(dung_t_year, 1),
        "compost_tonnes_per_year": round(compost_t_year, 1),
        "acres_manured_per_year": round(acres_fed, 1),
        "biogas_m3_per_day": round(biogas_m3_day, 2),
        "notes": [manure_value_note, res["assumptions_note"]],
    }
    if area_acres:
        out["manure_self_sufficiency_pct"] = round(min(100.0, acres_fed / area_acres * 100), 0)
    return out


def resilience_plan(cows: int | None, area_acres: float | None) -> dict:
    res = knowledge.load("resilience")
    plan = {"steps": res["steps"], "enterprises": res["enterprises"], "assumptions": res["assumptions_note"]}
    if cows:
        plan["livestock_estimate"] = livestock_estimate(cows, area_acres)
    elif area_acres:
        # Suggest a starting herd: ~1 cow's compost per 2.5 acres at default figures.
        d = res["defaults"]
        per_cow_acres = (d["dung_kg_per_cow_per_day"] * 365 / 1000 * d["compost_yield_fraction"]) / d["manure_t_per_acre_per_year"]
        plan["suggested_cows_for_manure_self_sufficiency"] = max(1, round(area_acres / per_cow_acres))
    return plan


# ---------------------------------------------------------------- water tips
def water_tips(crop: str | None, enso: EnsoState | None, dry: bool = False, wet: bool = False, hot: bool = False) -> list[dict]:
    tags = {"always"}
    if dry:
        tags.add("dry")
    if wet:
        tags.add("wet")
    if hot:
        tags.add("hot")
    if enso and enso.available and enso.phase == "el_nino":
        tags |= {"el_nino", "dry"}
    out = []
    for t in knowledge.load("water_tips")["tips"]:
        if not tags.intersection(t["when"]):
            continue
        if t["crops"] and crop and crop not in t["crops"]:
            continue
        out.append({k: v for k, v in t.items() if k not in ("when", "crops")} | {"priority": len(tags.intersection(t["when"]))})
    out.sort(key=lambda t: -t["priority"])
    return out


# ---------------------------------------------------------------- market / value-add
def market_advice(country: str, crop: str, has_livestock: bool = False) -> dict:
    va = knowledge.load("value_add")
    schemes = [s for s in knowledge.load("schemes")["schemes"] if s["id"] in va["linked_schemes"] and s["country"] == country.upper()]
    out = {
        "crop": crop,
        "value_addition_ideas": va["crops"].get(crop, []),
        "principles": va["generic"]["principles"],
        "market_channels": va["generic"]["market_channels"].get(country.upper(), []),
        "support_schemes": schemes,
    }
    if has_livestock:
        out["livestock_value_addition"] = va["livestock"]
    return out
