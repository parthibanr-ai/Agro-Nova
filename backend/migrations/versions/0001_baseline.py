"""Baseline: users, plots and soil samples as they were before migrations existed.

Databases created earlier with `create_all` already have these tables; app.db.migrate() adopts them by stamping
this revision instead of running it.

Revision ID: 0001
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("uid", sa.String(128), primary_key=True),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("state", sa.String(80), nullable=True),
        sa.Column("fcm_token", sa.String(300), nullable=True),
        sa.Column("livestock", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "plots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_uid", sa.String(128), sa.ForeignKey("users.uid"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("state", sa.String(80), nullable=True),
        sa.Column("crop", sa.String(40), nullable=False),
        sa.Column("sowing_date", sa.Date(), nullable=True),
        sa.Column("corners", sa.JSON(), nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column("centroid_lat", sa.Float(), nullable=False),
        sa.Column("centroid_lon", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plots_owner_uid", "plots", ["owner_uid"])
    op.create_table(
        "soil_samples",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("plot_id", sa.String(32), sa.ForeignKey("plots.id"), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("sampled_on", sa.Date(), nullable=True),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_soil_samples_plot_id", "soil_samples", ["plot_id"])


def downgrade() -> None:
    op.drop_index("ix_soil_samples_plot_id", table_name="soil_samples")
    op.drop_table("soil_samples")
    op.drop_index("ix_plots_owner_uid", table_name="plots")
    op.drop_table("plots")
    op.drop_table("users")
