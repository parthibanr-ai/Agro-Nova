from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")
_kwargs: dict = (
    {"connect_args": {"check_same_thread": False}}
    if _is_sqlite
    else {
        "pool_size": _settings.db_pool_size,
        "max_overflow": _settings.db_max_overflow,
        "pool_timeout": _settings.db_pool_timeout_s,
        "pool_pre_ping": True,  # drop connections the server or a proxy has silently closed
    }
)
if _settings.database_url in ("sqlite://", "sqlite:///:memory:"):
    _kwargs["poolclass"] = StaticPool

engine = create_engine(_settings.database_url, **_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
