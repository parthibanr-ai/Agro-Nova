"""Nightly satellite/climate snapshots per plot, so the API reads a row instead of calling Earth Engine.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "climate_snapshots",
        sa.Column("plot_key", sa.String(40), primary_key=True),
        sa.Column("window_days", sa.Integer(), primary_key=True),
        sa.Column("plot_id", sa.String(32), nullable=True),
        sa.Column("computed_on", sa.Date(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("rain_mm", sa.Float(), nullable=True),
        sa.Column("rain_normal_mm", sa.Float(), nullable=True),
        sa.Column("tmean_c", sa.Float(), nullable=True),
        sa.Column("ndvi", sa.Float(), nullable=True),
        sa.Column("ndvi_normal", sa.Float(), nullable=True),
        sa.Column("soil_moisture_pct", sa.Float(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_climate_snapshots_plot_id", "climate_snapshots", ["plot_id"])
    op.create_index("ix_climate_snapshots_computed_on", "climate_snapshots", ["computed_on"])


def downgrade() -> None:
    op.drop_index("ix_climate_snapshots_computed_on", table_name="climate_snapshots")
    op.drop_index("ix_climate_snapshots_plot_id", table_name="climate_snapshots")
    op.drop_table("climate_snapshots")
