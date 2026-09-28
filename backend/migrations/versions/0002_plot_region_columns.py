"""Plots get a district and a grid cell id, so farms can be grouped and indexed by area.

The backfill writes the same cell id as app/domain/grid.py::cell_id for every existing plot. The formula is
inlined (0.05 degree grid) on purpose: a migration must keep producing the same result even if the app's helper
changes later.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

CELL_DEG = 0.05
BATCH = 1000


def _part(v: float) -> str:
    n = round(v / CELL_DEG)
    return f"m{-n}" if n < 0 else str(n)


def upgrade() -> None:
    op.add_column("plots", sa.Column("district", sa.String(80), nullable=True))
    op.add_column("plots", sa.Column("cell_id", sa.String(24), nullable=True))

    # Backfill in batches so a large table is never loaded into memory at once.
    conn = op.get_bind()
    plots = sa.table("plots", sa.column("id", sa.String), sa.column("centroid_lat", sa.Float),
                     sa.column("centroid_lon", sa.Float), sa.column("cell_id", sa.String))
    last_id = ""
    while True:
        rows = conn.execute(
            sa.select(plots.c.id, plots.c.centroid_lat, plots.c.centroid_lon)
            .where(plots.c.id > last_id).order_by(plots.c.id).limit(BATCH)
        ).all()
        if not rows:
            break
        for pid, lat, lon in rows:
            conn.execute(plots.update().where(plots.c.id == pid).values(cell_id=f"{_part(lat)}_{_part(lon)}"))
        last_id = rows[-1][0]

    op.create_index("ix_plots_cell_id", "plots", ["cell_id"])
    op.create_index("ix_plots_segment", "plots", ["country", "state", "crop"])


def downgrade() -> None:
    op.drop_index("ix_plots_segment", table_name="plots")
    op.drop_index("ix_plots_cell_id", table_name="plots")
    with op.batch_alter_table("plots") as batch:
        batch.drop_column("cell_id")
        batch.drop_column("district")
