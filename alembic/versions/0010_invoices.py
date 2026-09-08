"""Add invoices table and event_configs.next_invoice_number counter.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-08

Milestone 5 (Invoicing): ``invoices`` holds one issued Invoice per Order
(1:1, ``order_id`` unique) — a snapshot of company/VAT/line-item data at
issue time plus the allocated sequential ``number``, per
``app.models.invoice.Invoice``'s docstring for why this is a snapshot
rather than a live reference to ``EventConfig``/``TicketType``.
``event_configs.next_invoice_number`` is the atomic per-event running
counter backing sequential numbering (row-locked and incremented by
``app.services.invoicing._allocate_invoice_number``, mirroring
``app.services.stock.reserve_stock``'s locking discipline) — defaults to
``1`` so every existing EventConfig row starts a fresh event-scoped
sequence at its first invoice.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_configs",
        sa.Column("next_invoice_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("event_configs", "next_invoice_number", server_default=None)

    op.create_table(
        "invoices",
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("number_prefix", sa.String(length=50), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("company_address", sa.Text(), nullable=True),
        sa.Column("company_vat_number", sa.String(length=50), nullable=True),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_invoices_order_id"),
        sa.UniqueConstraint("event_id", "number", name="uq_invoice_event_number"),
    )
    op.create_index("ix_invoices_event_id", "invoices", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_invoices_event_id", table_name="invoices")
    op.drop_table("invoices")
    op.drop_column("event_configs", "next_invoice_number")
