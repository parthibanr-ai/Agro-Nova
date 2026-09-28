from datetime import date

from pydantic import BaseModel, Field


class Corner(BaseModel):
    lat: float
    lon: float


class PlotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    country: str = Field("IN", min_length=2, max_length=2)
    state: str | None = None
    district: str | None = Field(None, max_length=80)
    crop: str
    sowing_date: date | None = None
    corners: list[Corner] = Field(min_length=4, max_length=12, description="Boundary corners in walking order")


class ProfileUpdate(BaseModel):
    language: str | None = None
    country: str | None = None
    state: str | None = None
    livestock: dict | None = None


class FcmToken(BaseModel):
    token: str


class SoilSampleCreate(BaseModel):
    lat: float
    lon: float
    source: str = Field("soil_health_card", description="soil_health_card | lab_test | field_kit | other")
    sampled_on: date | None = None
    values: dict = Field(description="e.g. {'ph': 6.8, 'oc': 0.6, 'n': 240, 'p': 18, 'k': 190, 'ec': 0.4}")


class LivestockRequest(BaseModel):
    cows: int = Field(ge=1, le=200)
    area_acres: float | None = None
    milk_l_per_cow_per_day: float | None = None
    milk_price_per_l: float | None = None
    feed_cost_per_cow_per_day: float | None = None
