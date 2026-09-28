"""Consent records, retention clock and idempotent offline writes.

users.consents / consent_version / consented_at: what the farmer agreed to under which notice version.
users.last_seen_at: lets `python -m app.batch.retention` erase accounts unused for the retention period.
consent_events: append-only history of consent changes.
plots.client_ref, soil_samples.client_ref: a reference the app makes up when it saves offline, so a retried
upload returns the record already stored instead of creating a second one.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        # Existing farmers have not agreed to anything yet: an empty record means "ask them".
        batch.add_column(sa.Column("consents", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("consent_version", sa.String(20), nullable=True))
        batch.add_column(sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("plots") as batch:
        batch.add_column(sa.Column("client_ref", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_plots_client_ref", ["owner_uid", "client_ref"])
    with op.batch_alter_table("soil_samples") as batch:
        batch.add_column(sa.Column("client_ref", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_soil_samples_client_ref", ["plot_id", "client_ref"])
    op.create_table(
        "consent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("uid", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("notice_version", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_consent_events_uid", "consent_events", ["uid"])


def downgrade() -> None:
    op.drop_index("ix_consent_events_uid", table_name="consent_events")
    op.drop_table("consent_events")
    with op.batch_alter_table("soil_samples") as batch:
        batch.drop_constraint("uq_soil_samples_client_ref", type_="unique")
        batch.drop_column("client_ref")
    with op.batch_alter_table("plots") as batch:
        batch.drop_constraint("uq_plots_client_ref", type_="unique")
        batch.drop_column("client_ref")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_seen_at")
        batch.drop_column("consented_at")
        batch.drop_column("consent_version")
        batch.drop_column("consents")
