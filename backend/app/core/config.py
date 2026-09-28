import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ADMIN_API_KEY = "change-me"
MIN_ADMIN_KEY_LENGTH = 16


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

    # Shared store (Redis / Memorystore, single node). Optional: without REDIS_URL every instance keeps its own caches
    # and rate-limit counters. See app/core/shared_store.py.
    redis_url: str | None = None  # e.g. redis://10.0.0.3:6379/0
    redis_socket_timeout_s: float = 0.3  # a slow Redis must never make a request slow: give up and go local
    redis_max_connections: int = 50
    redis_down_backoff_s: int = 15  # after an error, skip Redis for this long
    shared_lock_ttl_s: int = 60  # how long one instance may hold "I am computing this" for a shared cache key
    shared_lock_wait_s: float = 20.0  # how long another instance waits for that result before computing it itself

    # Per-farmer request limits on Gemini-backed endpoints (counted across all instances when REDIS_URL is set,
    # otherwise each instance counts separately).
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
    admin_api_key: str = DEFAULT_ADMIN_API_KEY
    # Firebase App Check proves a request comes from the genuine app (Play Integrity / App Attest / reCAPTCHA), so a
    # script cannot mint unlimited anonymous accounts against the AI endpoints. Only used with AUTH_MODE=firebase.
    #   off      - do not look at it (default)
    #   monitor  - count valid/missing/invalid tokens in /metrics but let everything through: roll this out first,
    #              watch that real farmers show up as "valid", then switch to enforce
    #   enforce  - refuse requests without a valid token (403)
    app_check_mode: str = "off"

    # Earth Engine takes 10 to 30 s per plot. `python -m app.batch.climate_snapshot` computes every plot overnight in
    # bulk into the climate_snapshots table and the API reads that row instead. Snapshots older than this many days
    # are ignored (a stopped batch is noticed, not silently served for weeks).
    ee_snapshot_max_age_days: int = 2
    # A plot with no snapshot yet (added today): call Earth Engine live for it once (true), or skip satellite data
    # until tonight's batch and serve the shared Open-Meteo weather meanwhile (false; use this at national scale so a
    # burst of new plots cannot exhaust Earth Engine's quota).
    ee_live_fallback: bool = True
    ee_batch_chunk: int = 1000  # plots per Earth Engine request in the batch (2,500 took 34 s in a test; 500 took 22 s)
    bigquery_table: str | None = None  # "project.dataset.table": also append each night's rows here for analytics

    ee_auth_mode: str = "user"  # "service_account" | "user"
    gee_service_account_email: str | None = None
    gee_service_account_key_path: str | None = None
    gee_cloud_project: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_fallback_model: str | None = "gemini-3.6-flash,gemini-flash-lite-latest"
    gemini_use_vertex: bool = False
    # Which model vendor answers, tried in this order until one succeeds. A vendor with no key is skipped, so with
    # only GEMINI_API_KEY set this is Gemini alone. Names: openai, anthropic, gemini. Keys belong in .env only.
    llm_order: str = "openai,anthropic,gemini"
    llm_vendor_timeout_s: float = 20.0  # per call to OpenAI / Anthropic, so a slow vendor is passed over quickly
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"  # must accept images and JSON mode (diagnosis sends a photo)
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5-20251001"  # fast and accepts images
    anthropic_base_url: str = "https://api.anthropic.com/v1"
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
    task_workers: int = 4  # background task threads per instance (push-notification fan-out)

    # Where background work runs. "local": on this instance's own thread pools (fine for one instance or a pilot).
    # "cloudtasks": queued on Google Cloud Tasks, which calls back POST /api/v1/internal/tasks/{name} on whichever
    # instance is free, retries failures and rate-limits dispatch. Set up as described in docs/OPERATIONS.md.
    task_backend: str = "local"  # push-notification fan-out
    job_backend: str = "local"  # AI jobs (crop recommendation, diagnosis, the other AI screens, forecast)
    cloud_tasks_project: str | None = None  # defaults to GOOGLE_CLOUD_PROJECT / GEE_CLOUD_PROJECT
    cloud_tasks_location: str | None = None  # defaults to GCP_LOCATION
    cloud_tasks_queue: str = "agrin-tasks"
    cloud_tasks_job_queue: str = "agrin-jobs"  # separate, so a burst of AI jobs cannot starve the daily digests
    task_target_url: str | None = None  # this service's public https base URL, e.g. https://agrin-api-abc.a.run.app
    task_service_account: str | None = None  # the service account Cloud Tasks signs its OIDC token as
    task_audience: str | None = None  # OIDC audience; defaults to task_target_url
    # With job_backend=cloudtasks, uploaded photos wait here (a task body is limited to 100 KB). Give the bucket a
    # 1-day lifecycle rule; the worker also deletes each photo as soon as it is done with it.
    job_payload_bucket: str | None = None
    # A farmer hears about each scheme once, and at most this many new ones per day, so the daily push stays worth
    # opening instead of being muted. (A backlog of 11 matching schemes is spread over about six days.)
    notify_max_new_schemes_per_day: int = 2

    # Observability
    log_format: str = "text"  # "json" for Cloud Logging and other log platforms
    log_level: str = "INFO"
    metrics_enabled: bool = True  # GET /metrics (admin key), Prometheus format

    translate_api_key: str | None = None

    google_maps_api_key: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


def production_problems(s: "Settings") -> list[str]:
    """Settings that are fine on a laptop but unsafe once real farmers sign in (AUTH_MODE=firebase).

    The admin key guards /metrics, push-notification dispatch, job purging and the task endpoint, so a guessable
    one hands the internals of a national service to anyone who reads the source repository.
    """
    if s.auth_mode != "firebase":
        return []
    problems = []
    key = s.admin_api_key.strip()
    if key == DEFAULT_ADMIN_API_KEY or not key:
        problems.append("ADMIN_API_KEY is still the default value. Set a long random string, for example "
                        "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`.")
    elif len(key) < MIN_ADMIN_KEY_LENGTH:
        problems.append(f"ADMIN_API_KEY is shorter than {MIN_ADMIN_KEY_LENGTH} characters.")
    if s.app_check_mode not in ("off", "monitor", "enforce"):
        problems.append("APP_CHECK_MODE must be off, monitor or enforce.")
    return problems


def configuration_problems(s: "Settings") -> list[str]:
    """Settings that cannot work in any mode: a backend chosen without what it needs."""
    problems = []
    for name in ("task_backend", "job_backend"):
        if getattr(s, name) not in ("local", "cloudtasks"):
            problems.append(f"{name.upper()} must be local or cloudtasks.")
    if "cloudtasks" in (s.task_backend, s.job_backend):
        if not (s.cloud_tasks_project or os.environ.get("GOOGLE_CLOUD_PROJECT") or s.gee_cloud_project):
            problems.append("Cloud Tasks needs CLOUD_TASKS_PROJECT (or GOOGLE_CLOUD_PROJECT).")
        if not (s.task_target_url or "").startswith("https://"):
            problems.append("Cloud Tasks needs TASK_TARGET_URL, this service's public https:// address.")
        if not s.task_service_account:
            problems.append("Cloud Tasks needs TASK_SERVICE_ACCOUNT, the service account that signs its calls back to "
                            "this service.")
    if s.job_backend == "cloudtasks" and not s.job_payload_bucket:
        problems.append("JOB_BACKEND=cloudtasks needs JOB_PAYLOAD_BUCKET, where uploaded photos wait for a worker.")
    return problems


def check_production_settings(s: "Settings | None" = None) -> None:
    """Refuse to start with unsafe or incomplete settings (called at start-up)."""
    s = s or get_settings()
    problems = configuration_problems(s) + production_problems(s)
    if problems:
        raise RuntimeError("Refusing to start:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
