"""Schema migrations: they build the schema, match the models, adopt old databases and can be reversed."""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.db import Base, alembic_config, migrate
from app.domain.grid import cell_id, snap
from tests.conftest import NASHIK


@pytest.fixture()
def eng(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    yield e
    e.dispose()


def _run(engine, fn):
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        cfg.attributes["configure_logger"] = False
        fn(cfg)


def test_migrations_build_a_schema_that_matches_the_models(eng):
    """If someone changes a model without writing a migration, this fails (schema drift)."""
    migrate(eng)
    with eng.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diffs == [], f"models and migrations disagree: {diffs}"


def test_migrate_is_idempotent(eng):
    migrate(eng)
    migrate(eng)
    assert "plots" in inspect(eng).get_table_names()


def test_a_database_created_before_migrations_is_adopted_not_recreated(eng):
    _run(eng, lambda cfg: command.upgrade(cfg, "0001"))
    with eng.begin() as conn:
        conn.execute(sa.text("DROP TABLE alembic_version"))  # what a create_all-era database looks like
        conn.execute(sa.text("INSERT INTO users (uid, language, country, livestock, created_at) "
                             "VALUES ('u1', 'hi', 'IN', '{}', '2026-01-01')"))
    migrate(eng)  # must not fail with "table already exists", and must keep the data
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT language FROM users WHERE uid='u1'")).scalar() == "hi"
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == head
    assert "cell_id" in {c["name"] for c in inspect(eng).get_columns("plots")}


def test_existing_plots_get_their_cell_id_backfilled(eng):
    _run(eng, lambda cfg: command.upgrade(cfg, "0001"))
    rows = [("p1", 19.998, 73.7903), ("p2", -33.87, 151.21), ("p3", 30.9010, 75.8570)]  # incl. southern hemisphere
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (uid, language, country, livestock, created_at) "
                             "VALUES ('u', 'en', 'IN', '{}', '2026-01-01')"))
        for pid, lat, lon in rows:
            conn.execute(sa.text(
                "INSERT INTO plots (id, owner_uid, name, country, crop, corners, area_m2, centroid_lat, centroid_lon, created_at) "
                "VALUES (:id, 'u', 'n', 'IN', 'wheat', '[]', 1, :lat, :lon, '2026-01-01')"), {"id": pid, "lat": lat, "lon": lon})
    _run(eng, lambda cfg: command.upgrade(cfg, "head"))
    with eng.connect() as conn:
        got = dict(conn.execute(sa.text("SELECT id, cell_id FROM plots")).all())
    assert got == {pid: cell_id(lat, lon) for pid, lat, lon in rows}


def test_migrations_can_be_reversed_and_reapplied(eng):
    migrate(eng)
    _run(eng, lambda cfg: command.downgrade(cfg, "0001"))
    assert "cell_id" not in {c["name"] for c in inspect(eng).get_columns("plots")}
    _run(eng, lambda cfg: command.downgrade(cfg, "base"))
    assert "users" not in inspect(eng).get_table_names()
    migrate(eng)
    assert "cell_id" in {c["name"] for c in inspect(eng).get_columns("plots")}


# ------------------------------------------------------------------ grid cells
def test_nearby_points_share_a_cell_and_distant_ones_do_not():
    assert cell_id(30.9010, 75.8570) == cell_id(30.9040, 75.8600)
    assert cell_id(30.9010, 75.8570) != cell_id(31.2000, 75.8570)
    assert snap(30.9010) == 30.9 and cell_id(-33.87, 151.21).startswith("m")


def test_new_plots_record_their_cell_and_district(client):
    body = {"name": "P", "crop": "wheat", "country": "IN", "state": "Punjab", "district": "Ludhiana", "corners": NASHIK}
    made = client.post("/api/v1/plots", json=body).json()
    assert made["district"] == "Ludhiana"
    from app.db import SessionLocal
    from app.models import Plot

    with SessionLocal() as db:
        plot = db.get(Plot, made["id"])
        assert plot.cell_id == cell_id(plot.centroid_lat, plot.centroid_lon)


# ------------------------------------------------------------------ real Postgres (opt-in)
@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL to run against Postgres")
def test_migrations_and_first_login_race_on_real_postgres():
    """Run with e.g. TEST_POSTGRES_URL=postgresql://user:pass@localhost:5544/agrin_test (a scratch database)."""
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.orm import sessionmaker

    from app.db import get_db, normalize_database_url
    from app.main import app
    from fastapi.testclient import TestClient

    pg = create_engine(normalize_database_url(os.environ["TEST_POSTGRES_URL"]), pool_size=10, max_overflow=10)
    with pg.begin() as conn:  # start from nothing
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    migrate(pg)
    _run(pg, lambda cfg: command.downgrade(cfg, "base"))
    migrate(pg)
    with pg.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []

    make_session = sessionmaker(bind=pg, autoflush=False, expire_on_commit=False)

    def db_dep():
        db = make_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_dep
    try:
        client = TestClient(app, headers={"X-Dev-User": "pg-race-user"})
        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(lambda _: client.get("/api/v1/me").status_code, range(40)))
    finally:
        app.dependency_overrides.pop(get_db, None)
        pg.dispose()
    assert codes == [200] * 40
