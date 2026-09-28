import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.core.ratelimit import limit
from app.core.gee_auth import earth_engine_init_error, is_earth_engine_ready
from app.core.languages import LANGUAGES, get_language
from app.db import get_db
from app.domain.grid import cell_id
from app.domain.polygon import PolygonValidationError, centroid, polygon_area_m2, validate_plot
from app.models import Plot, SoilSample, User
from app.providers.base import ObservedClimate
from app.providers.registry import get_climate_provider
from app.schemas import FcmToken, LivestockRequest, PlotCreate, ProfileUpdate, SoilSampleCreate
from app.services import advice, crop_recommendation, diagnosis, geocode, knowledge, personalized_advice, soil
from app.services.enso import get_enso_state
from app.services.forecast import build_forecast_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _release_db(db: Session) -> None:
    """End the read transaction so its pooled connection is free while we wait on a slow external service.

    Rows already loaded stay usable (sessions are created with expire_on_commit=False); anything loaded later
    simply opens a new short transaction. Without this, every AI/satellite request holds one of a small number of
    pooled connections for the whole time Gemini or Earth Engine takes to answer.
    """
    db.commit()


def _plot_or_404(db: Session, user: User, plot_id: str) -> Plot:
    plot = db.get(Plot, plot_id)
    if plot is None or plot.owner_uid != user.uid:
        raise HTTPException(404, "Plot not found")
    return plot


def _plot_json(p: Plot) -> dict:
    return {
        "id": p.id, "name": p.name, "country": p.country, "state": p.state, "district": p.district, "crop": p.crop,
        "sowing_date": p.sowing_date.isoformat() if p.sowing_date else None,
        "corners": p.corners, "area_m2": round(p.area_m2, 1), "area_acres": round(p.area_m2 / 4046.856, 2),
        "area_ha": round(p.area_m2 / 10_000, 3),
        "centroid": {"lat": p.centroid_lat, "lon": p.centroid_lon},
    }


# ------------------------------------------------------------------ meta
@router.get("/health")
def health() -> dict:
    return {"status": "ok", "earth_engine": is_earth_engine_ready(), "earth_engine_error": earth_engine_init_error()}


@router.get("/meta/languages")
def languages() -> dict:
    return {"languages": [
        {"code": lang.code, "name": lang.name, "native_name": lang.native_name, "group": lang.group,
         "machine_translation": lang.machine_translation}
        for lang in LANGUAGES]}


@router.get("/meta/countries")
def countries() -> dict:
    return {"countries": [
        {"code": code, "name": c["name"], "default_language": c["default_language"], "currency": c["currency"]}
        for code, c in knowledge.countries().items()]}


@router.get("/meta/crops")
def crops() -> dict:
    return {"crops": [{"id": cid, "name": c["name"]} for cid, c in knowledge.crops().items()]}


@router.get("/geocode")
def geocode_search(q: str) -> dict:
    return {"results": geocode.search(q)}


# ------------------------------------------------------------------ profile
@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"uid": user.uid, "language": user.language, "country": user.country, "state": user.state, "livestock": user.livestock}


@router.put("/me")
def update_me(body: ProfileUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if body.language:
        user.language = get_language(body.language).code
    if body.country:
        try:
            knowledge.country(body.country)
        except KeyError as e:
            raise HTTPException(422, str(e)) from e
        user.country = body.country.upper()
    if body.state is not None:
        user.state = body.state
    if body.livestock is not None:
        user.livestock = body.livestock
    db.merge(user)
    db.commit()
    return me(user)


@router.post("/me/fcm-token", status_code=204)
def register_token(body: FcmToken, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    user.fcm_token = body.token
    db.merge(user)
    db.commit()


# ------------------------------------------------------------------ plots
@router.post("/plots", status_code=201)
def create_plot(body: PlotCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        country = knowledge.country(body.country)
        knowledge.crop(body.crop)
    except KeyError as e:
        raise HTTPException(422, str(e)) from e
    points = [(c.lat, c.lon) for c in body.corners]
    try:
        validate_plot(points, tuple(country["bbox"]))
    except PolygonValidationError as e:
        raise HTTPException(422, str(e)) from e
    clat, clon = centroid(points)
    plot = Plot(
        owner_uid=user.uid, name=body.name, country=body.country.upper(), state=body.state or user.state,
        district=body.district, cell_id=cell_id(clat, clon), crop=body.crop, sowing_date=body.sowing_date, corners=[c.model_dump() for c in body.corners],
        area_m2=polygon_area_m2(points), centroid_lat=clat, centroid_lon=clon,
    )
    db.add(plot)
    if user.primary_crop is None:  # their first plot decides which notifications they get
        user.primary_crop = plot.crop
        if user.state is None and plot.state:
            user.state = plot.state  # so state-specific schemes reach farmers who never filled in a profile state
        db.merge(user)
    db.commit()
    return _plot_json(plot)


@router.get("/plots")
def list_plots(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plots = db.query(Plot).filter(Plot.owner_uid == user.uid).order_by(Plot.created_at).all()
    return {"plots": [_plot_json(p) for p in plots]}


@router.get("/plots/{plot_id}")
def get_plot(plot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _plot_json(_plot_or_404(db, user, plot_id))


@router.delete("/plots/{plot_id}", status_code=204)
def delete_plot(plot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    plot = _plot_or_404(db, user, plot_id)
    db.delete(plot)
    db.flush()
    user.primary_crop = db.scalars(select(Plot.crop).where(Plot.owner_uid == user.uid)
                                   .order_by(Plot.created_at, Plot.id).limit(1)).first()
    db.merge(user)
    db.commit()


# ------------------------------------------------------------------ soil
@router.get("/plots/{plot_id}/soil")
def get_soil(plot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id)
    samples = sorted(plot.soil_samples, key=lambda s: s.created_at, reverse=True)
    _release_db(db)
    public = None if samples else soil.fetch_soilgrids(plot.centroid_lat, plot.centroid_lon)
    values = samples[0].values if samples else (public or {})
    return {
        "plot_id": plot.id,
        "farmer_samples": [
            {"id": s.id, "lat": s.lat, "lon": s.lon, "source": s.source,
             "sampled_on": s.sampled_on.isoformat() if s.sampled_on else None, "values": s.values}
            for s in samples],
        "public_data": public,
        "public_data_source": "ISRIC SoilGrids 2.0 (250 m, modelled)" if public else None,
        "observations": soil.interpret(values),
        "prompt": soil.acquisition_prompt(plot.country, bool(samples)),
    }


@router.post("/plots/{plot_id}/soil", status_code=201)
def add_soil_sample(plot_id: str, body: SoilSampleCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id)
    if not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180):
        raise HTTPException(422, "Sample latitude/longitude out of range")
    sample = SoilSample(plot_id=plot.id, lat=body.lat, lon=body.lon, source=body.source, sampled_on=body.sampled_on, values=body.values)
    db.add(sample)
    db.commit()
    return {"id": sample.id, "observations": soil.interpret(body.values)}


# ------------------------------------------------------------------ weather / ENSO
@router.get("/enso")
def enso() -> dict:
    return get_enso_state().to_dict()


@router.get("/plots/{plot_id}/forecast")
def forecast(plot_id: str, days: int = 10, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id)
    _release_db(db)
    provider = get_climate_provider()
    try:
        observed = provider.observed(plot.corner_points, 30)
    except Exception:  # noqa: BLE001 - keep the forecast useful even if history is unavailable
        observed = None
    try:
        daily = provider.forecast(plot.centroid_lat, plot.centroid_lon, min(max(days, 1), 16))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "Weather forecast is temporarily unavailable") from e
    return build_forecast_report(plot=plot, observed=observed, daily=daily, enso=get_enso_state())


def _plot_soil_and_climate(db: Session, plot: Plot) -> tuple[dict, ObservedClimate | None]:
    """Best-effort soil values + recent observed satellite/climate for one plot, for LLM grounding."""
    samples = sorted(plot.soil_samples, key=lambda s: s.created_at, reverse=True)
    _release_db(db)
    provider = get_climate_provider()
    try:
        observed = provider.observed(plot.corner_points, 30)
    except Exception:  # noqa: BLE001 - keep advice useful even if history is unavailable
        observed = None
    soil_values = samples[0].values if samples else (soil.fetch_soilgrids(plot.centroid_lat, plot.centroid_lon) or {})
    return soil_values, observed


@router.get("/plots/{plot_id}/crop-recommendation", dependencies=[Depends(limit("ai"))])
def crop_recommendation_for_plot(plot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id)
    soil_values, observed = _plot_soil_and_climate(db, plot)
    try:
        result = crop_recommendation.recommend(
            country_code=plot.country, state=plot.state, area_ha=plot.area_m2 / 10_000, current_crop=plot.crop,
            soil_values=soil_values, observed=observed, enso=get_enso_state(),
        )
    except crop_recommendation.RecommendationUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("crop recommendation failed")
        raise HTTPException(502, "The recommendation service could not respond. Try again.") from e
    result["plot_id"] = plot.id
    return result


# ------------------------------------------------------------------ diagnosis
@router.post("/diagnosis", dependencies=[Depends(limit("diagnosis"))])
def diagnose_plant(
    image: UploadFile = File(...),
    crop: str | None = Form(None),
    notes: str | None = Form(None),
    plot_id: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    # A plain `def` route runs in FastAPI's worker-thread pool, so the slow, blocking Gemini call below cannot
    # stall the event loop that every other request depends on.
    data = read_photo(image)
    return run_diagnosis(db, user, data, image.content_type, crop, notes, plot_id)


def read_photo(image: UploadFile) -> bytes:
    """Validate and read an uploaded photo (type and size limits shared by the direct and job endpoints)."""
    if image.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(415, "Upload a JPEG, PNG or WebP photo")
    data = image.file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Photo is larger than 8 MB")
    return data


def run_diagnosis(db: Session, user: User, data: bytes, content_type: str, crop: str | None, notes: str | None,
                  plot_id: str | None) -> dict:
    """Analyse one photo. Used by the direct endpoint and by the diagnosis job worker."""
    if plot_id:
        crop = crop or _plot_or_404(db, user, plot_id).crop
    country = user.country
    _release_db(db)
    try:
        return diagnosis.diagnose(data, content_type, crop, country, notes)
    except diagnosis.DiagnosisUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "The diagnosis service could not analyse this photo. Try again.") from e


# ------------------------------------------------------------------ schemes, resilience, water, market
@router.get("/schemes")
def schemes(plot_id: str | None = None, category: str | None = None,
            user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id) if plot_id else None
    return advice.schemes_response(user.country, user.state, plot.crop if plot else None,
                                   plot.area_m2 / 10_000 if plot else None, category)


def _personalized_advice(db: Session, kind: str, plot: Plot | None, user: User) -> dict | None:
    """Hyper-personalised LLM advice grounded in this plot's soil/climate/ENSO data, or None without a plot."""
    if plot is None:
        return None
    soil_values, observed = _plot_soil_and_climate(db, plot)
    try:
        return personalized_advice.advise(
            kind, country_code=plot.country, state=plot.state, area_ha=plot.area_m2 / 10_000,
            current_crop=plot.crop, soil_values=soil_values, observed=observed, enso=get_enso_state(),
            has_livestock=bool((user.livestock or {}).get("cows")),
        )
    except personalized_advice.AdviceUnavailable:
        return None
    except Exception:  # noqa: BLE001 - a transient LLM/network failure should drop this section, not the page
        logger.warning("Personalized %s advice failed for plot %s", kind, plot.id, exc_info=True)
        return None


@router.get("/resilience", dependencies=[Depends(limit("ai"))])
def resilience(plot_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id) if plot_id else None
    cows = (user.livestock or {}).get("cows")
    plan = advice.resilience_plan(cows, plot.area_m2 / 4046.856 if plot else None)
    plan["personalized"] = _personalized_advice(db, "resilience", plot, user)
    return plan


@router.post("/resilience/livestock-estimate")
def livestock_estimate(body: LivestockRequest, user: User = Depends(current_user)) -> dict:
    return advice.livestock_estimate(
        body.cows, body.area_acres, milk_l_per_cow_per_day=body.milk_l_per_cow_per_day,
        milk_price_per_l=body.milk_price_per_l, feed_cost_per_cow_per_day=body.feed_cost_per_cow_per_day)


@router.get("/water-tips", dependencies=[Depends(limit("ai"))])
def water_tips(plot_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id) if plot_id else None
    return {
        "tips": advice.water_tips(plot.crop if plot else None, get_enso_state()),
        "personalized": _personalized_advice(db, "water", plot, user),
    }


@router.get("/plots/{plot_id}/market", dependencies=[Depends(limit("ai"))])
def market(plot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    plot = _plot_or_404(db, user, plot_id)
    out = advice.market_advice(plot.country, plot.crop, bool((user.livestock or {}).get("cows")))
    out["personalized"] = _personalized_advice(db, "market", plot, user)
    return out
