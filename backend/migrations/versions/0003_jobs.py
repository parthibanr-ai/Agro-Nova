"""Jobs: slow AI requests that are accepted immediately and finished by a worker.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_uid", sa.String(128), sa.ForeignKey("users.uid"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_owner_status", "jobs", ["owner_uid", "status"])
    op.create_index("ix_jobs_fingerprint", "jobs", ["fingerprint"])
    op.create_index("ix_jobs_expires_at", "jobs", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_expires_at", table_name="jobs")
    op.drop_index("ix_jobs_fingerprint", table_name="jobs")
    op.drop_index("ix_jobs_owner_status", table_name="jobs")
    op.drop_table("jobs")
