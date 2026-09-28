import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    # Push digests walk one audience (country, state, first crop, language) in uid order, only among farmers with a
    # device token. This partial index makes each page of 500 a short range scan however many farmers exist.
    __table_args__ = (
        Index("ix_users_segment", "country", "state", "primary_crop", "language", "uid",
              postgresql_where=text("fcm_token IS NOT NULL"), sqlite_where=text("fcm_token IS NOT NULL")),
    )

    uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    language: Mapped[str] = mapped_column(String(10), default="en")
    country: Mapped[str] = mapped_column(String(2), default="IN")
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fcm_token: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Crop of the farmer's earliest plot (kept up to date when plots are added or deleted). It decides which
    # notifications they get, and storing it here means audiences never need a join over all plots.
    primary_crop: Mapped[str | None] = mapped_column(String(40), nullable=True)
    livestock: Mapped[dict] = mapped_column(JSON, default=dict)  # {"cows": 2, ...} for resilience advice
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    plots: Mapped[list["Plot"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Plot(Base):
    __tablename__ = "plots"
    # Regional digests and reports group by (country, state, crop); keep that scan an index lookup.
    __table_args__ = (Index("ix_plots_segment", "country", "state", "crop"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_uid: Mapped[str] = mapped_column(ForeignKey("users.uid"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2))
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    district: Mapped[str | None] = mapped_column(String(80), nullable=True)
    crop: Mapped[str] = mapped_column(String(40))
    sowing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    corners: Mapped[list] = mapped_column(JSON)  # [{"lat":..,"lon":..}, ...]
    area_m2: Mapped[float] = mapped_column(Float)
    centroid_lat: Mapped[float] = mapped_column(Float)
    centroid_lon: Mapped[float] = mapped_column(Float)
    cell_id: Mapped[str | None] = mapped_column(String(24), index=True, nullable=True)  # see app/domain/grid.py
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


class NotificationRun(Base):
    """One audience's push digest for one day. The unique (run_date, segment_key) is the lock that makes a
    retried or duplicated task safe: only the first claimant sends."""

    __tablename__ = "notification_runs"
    __table_args__ = (UniqueConstraint("run_date", "segment_key", name="uq_notification_run"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_date: Mapped[date] = mapped_column(Date)
    segment_key: Mapped[str] = mapped_column(String(200))  # "IN|Punjab|wheat|hi"
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | done | failed
    users: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    invalid_tokens: Mapped[int] = mapped_column(Integer, default=0)  # dead device tokens cleared this run
    refs: Mapped[list] = mapped_column(JSON, default=list)  # scheme ids announced, so they are not repeated
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Base):
    """A slow AI request (crop recommendation, photo diagnosis) accepted now and finished by a worker.

    The row is the shared state: any instance can answer "is my job done?", while the work itself runs on the
    instance that accepted it. Rows expire (expires_at) and are purged, so the table stays small.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_owner_status", "owner_uid", "status"),
        Index("ix_jobs_fingerprint", "fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_uid: Mapped[str] = mapped_column(ForeignKey("users.uid"))
    kind: Mapped[str] = mapped_column(String(40))  # "crop_recommendation" | "diagnosis"
    fingerprint: Mapped[str] = mapped_column(String(64))  # hash of (owner, kind, inputs): spots double-taps and retries
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | running | done | failed
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"status": 503, "message": "..."}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ClimateSnapshot(Base):
    """One plot's satellite and climate conditions, computed overnight in bulk by `python -m app.batch.climate_snapshot`.

    Earth Engine is an analytics system: one plot takes 10 to 30 seconds, but thousands of plots in one request take
    about the same time per batch. So the app computes everything nightly and the API only reads this table. The
    key is a hash of the plot's rounded corners (not the plot id), so editing a plot's shape never serves the old
    shape's numbers, and the same physical field registered twice shares one row.
    """

    __tablename__ = "climate_snapshots"
    __table_args__ = (Index("ix_climate_snapshots_computed_on", "computed_on"),)

    plot_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    window_days: Mapped[int] = mapped_column(Integer, primary_key=True)
    plot_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)  # erased with its plot
    computed_on: Mapped[date] = mapped_column(Date)
    as_of: Mapped[date | None] = mapped_column(Date, nullable=True)  # last day the rain window covers
    rain_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain_normal_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmean_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndvi: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndvi_normal: Mapped[float | None] = mapped_column(Float, nullable=True)
    soil_moisture_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
