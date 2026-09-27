import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    language: Mapped[str] = mapped_column(String(10), default="en")
    country: Mapped[str] = mapped_column(String(2), default="IN")
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fcm_token: Mapped[str | None] = mapped_column(String(300), nullable=True)
    livestock: Mapped[dict] = mapped_column(JSON, default=dict)  # {"cows": 2, ...} for resilience advice
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    plots: Mapped[list["Plot"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Plot(Base):
    __tablename__ = "plots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_uid: Mapped[str] = mapped_column(ForeignKey("users.uid"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2))
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    crop: Mapped[str] = mapped_column(String(40))
    sowing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    corners: Mapped[list] = mapped_column(JSON)  # [{"lat":..,"lon":..}, ...]
    area_m2: Mapped[float] = mapped_column(Float)
    centroid_lat: Mapped[float] = mapped_column(Float)
    centroid_lon: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    owner: Mapped[User] = relationship(back_populates="plots")
    soil_samples: Mapped[list["SoilSample"]] = relationship(back_populates="plot", cascade="all, delete-orphan")

    @property
    def corner_points(self) -> list[tuple[float, float]]:
        return [(c["lat"], c["lon"]) for c in self.corners]


class SoilSample(Base):
    """Farmer-provided soil data (e.g. from a Soil Health Card or a new field test)."""

    __tablename__ = "soil_samples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    plot_id: Mapped[str] = mapped_column(ForeignKey("plots.id"), index=True)
    lat: Mapped[float] = mapped_column(Float)  # where the sample was taken
    lon: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(60))  # "soil_health_card" | "lab_test" | "field_kit" | ...
    sampled_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    values: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    plot: Mapped[Plot] = relationship(back_populates="soil_samples")
