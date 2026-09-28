"""Notification runs (one row per audience per day) and an index for farmers with a device token.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The crop of each farmer's earliest plot, so push audiences need no join over all plots.
    op.add_column("users", sa.Column("primary_crop", sa.String(40), nullable=True))
    op.execute(
        "UPDATE users SET primary_crop = (SELECT p.crop FROM plots p WHERE p.owner_uid = users.uid "
        "ORDER BY p.created_at, p.id LIMIT 1)"
    )
    op.create_table(
        "notification_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("segment_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("users", sa.Integer(), nullable=False),
        sa.Column("sent", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("invalid_tokens", sa.Integer(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_date", "segment_key", name="uq_notification_run"),
    )
    op.create_index("ix_users_segment", "users", ["country", "state", "primary_crop", "language", "uid"],
                    postgresql_where=sa.text("fcm_token IS NOT NULL"), sqlite_where=sa.text("fcm_token IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_users_segment", table_name="users")
    op.drop_table("notification_runs")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("primary_crop")
