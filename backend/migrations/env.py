"""Alembic environment: the database URL and metadata come from the application itself."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app import models  # noqa: F401  (register tables on Base.metadata)
from app.db import DATABASE_URL, Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=DATABASE_URL.startswith("sqlite"),  # SQLite cannot ALTER most things in place
        **kw,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    _configure(url=DATABASE_URL, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")  # supplied by app.db.migrate() and by tests
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(DATABASE_URL, poolclass=NullPool)
    with engine.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
