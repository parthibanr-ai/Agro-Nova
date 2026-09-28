from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "0001"  # the schema as it was before migrations existed


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    """Accept the plain URLs hosting providers hand out (postgres://, postgresql://) and use the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


_settings = get_settings()
DATABASE_URL = normalize_database_url(_settings.database_url)
_is_sqlite = DATABASE_URL.startswith("sqlite")
_kwargs: dict = {}
if _is_sqlite:
    _kwargs["connect_args"] = {"check_same_thread": False}
else:
    _kwargs.update(
        pool_size=_settings.db_pool_size,
        max_overflow=_settings.db_max_overflow,
        pool_timeout=_settings.db_pool_timeout_s,
        pool_pre_ping=True,  # drop connections the server or a proxy has silently closed
    )
    if _settings.db_pgbouncer:
        # PgBouncer in transaction mode hands each transaction a different server connection, so psycopg must not
        # keep server-side prepared statements.
        _kwargs["connect_args"] = {"prepare_threshold": None}
if DATABASE_URL in ("sqlite://", "sqlite:///:memory:"):
    _kwargs["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg


def migrate(target_engine=None) -> None:
    """Bring the database schema up to date (idempotent). Defaults to the application's own engine.

    On Postgres run this as a single release step (`python -m app.migrate`), not from every instance at once; see
    AUTO_MIGRATE in docs/env_reference.md.
    """
    from alembic import command

    import app.models  # noqa: F401  (register tables)

    cfg = alembic_config()
    with (target_engine or engine).begin() as conn:
        cfg.attributes["connection"] = conn  # run on our own engine, so in-memory test databases work too
        cfg.attributes["configure_logger"] = False
        tables = inspect(conn).get_table_names()
        if "alembic_version" not in tables and "users" in tables:
            # A database created before migrations existed: adopt it at the baseline instead of re-creating tables.
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")


def init_db() -> None:
    """Called at start-up. Migrates automatically for SQLite/dev; production Postgres runs `python -m app.migrate`
    once per release instead (AUTO_MIGRATE=false), so a fleet of instances never migrates concurrently."""
    auto = _settings.auto_migrate if _settings.auto_migrate is not None else _is_sqlite
    if auto:
        migrate()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
