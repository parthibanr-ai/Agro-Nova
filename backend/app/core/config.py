from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./agrin.db"
    # Connection pool for Postgres etc. (ignored for SQLite). Size it so (instances x pool_size + max_overflow)
    # stays under the database's connection limit; put PgBouncer in front for large fleets.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: int = 10
    db_pgbouncer: bool = False  # true when a PgBouncer in transaction mode sits in front of Postgres
    # Run migrations at start-up. Default: yes for SQLite/dev, no for Postgres (run `python -m app.migrate` once per
    # release, so a fleet of instances does not migrate at the same time).
    auto_migrate: bool | None = None
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    default_country: str = "IN"

    # Caching (in-process; see app/core/cache.py). Set CACHE_ENABLED=false to bypass every cache.
    cache_enabled: bool = True
    climate_cache_ttl_s: int = 6 * 3600  # satellite/weather history for a ~5 km cell
    forecast_cache_ttl_s: int = 3600
    soil_cache_ttl_s: int = 30 * 24 * 3600  # SoilGrids is a static modelled dataset
    ai_cache_ttl_s: int = 24 * 3600  # Gemini advice shared by farmers with the same conditions
    translation_cache_max_entries: int = 200_000

    # Per-farmer request limits on Gemini-backed endpoints (each instance counts separately).
    rate_limit_enabled: bool = True
    ai_rate_per_minute: int = 20
    ai_rate_per_day: int = 300
    diagnosis_rate_per_minute: int = 5
    diagnosis_rate_per_day: int = 40
    # Deliberately high: mobile carriers put thousands of farmers behind one IPv4 address (carrier-grade NAT), so a
    # tight per-IP cap would throttle honest users. It only stops a single machine flooding the API; the per-farmer
    # limits above do the fine-grained work.
    ip_rate_per_minute: int = 600
    trust_forwarded_for: bool = False  # true only behind a proxy that sets X-Forwarded-For (Cloud Run does)

    auth_mode: str = "dev"  # "dev" | "firebase"
    firebase_credentials_path: str | None = None
    admin_api_key: str = "change-me"

    ee_auth_mode: str = "user"  # "service_account" | "user"
    gee_service_account_email: str | None = None
    gee_service_account_key_path: str | None = None
    gee_cloud_project: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_fallback_model: str | None = "gemini-3.6-flash,gemini-flash-lite-latest"
    gemini_use_vertex: bool = False
    gcp_location: str = "asia-south1"
    # At most this many Gemini calls in flight per instance, so N instances stay inside the project's quota
    # (N x this value). Extra requests wait up to the timeout, then get a "busy, try again" answer.
    gemini_max_concurrency: int = 16
    gemini_queue_timeout_s: float = 15.0

    # Slow AI requests (crop recommendation, photo diagnosis) run as jobs: accepted at once, finished by a worker.
    job_workers: int = 8  # worker threads per instance
    job_queue_max: int = 500  # accepted-but-unfinished jobs per instance before new ones are turned away
    job_fast_wait_s: float = 2.0  # how long the request waits for a quick answer before replying "accepted"
    job_ttl_s: int = 3600  # finished jobs are kept this long for polling, then purged
    job_lease_s: int = 180  # a job unfinished after this long is presumed lost (instance died) and marked failed

    translate_api_key: str | None = None

    google_maps_api_key: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
