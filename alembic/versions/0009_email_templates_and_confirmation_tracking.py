"""Add email_templates table and orders.confirmation_email_sent_at.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-08

Milestone 4 (Ticket generation & delivery): ``email_templates`` holds an
Event's per-language, per-type editable subject/body content (see
``app.models.email_template.EmailTemplate``) — ``language``/``template_type``
are plain string columns (not native Postgres enums), matching
``orders.language``'s existing choice, so adding a new supported language or
email type later is a Python-only change, no migration.
``orders.confirmation_email_sent_at`` is the send-tracking column
``app.services.ticket_delivery.send_order_confirmation_email`` updates on
every successful send (initial or resend) — see that column's docstring on
``app.models.order.Order``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("template_type", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "language", "template_type", name="uq_email_template_event_lang_type"),
    )
    op.create_index("ix_email_templates_event_id", "email_templates", ["event_id"])

    op.add_column("orders", sa.Column("confirmation_email_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "confirmation_email_sent_at")
    op.drop_index("ix_email_templates_event_id", table_name="email_templates")
    op.drop_table("email_templates")
