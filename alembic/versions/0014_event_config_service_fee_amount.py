"""Add event_configs.service_fee_amount: the flat per-ticket fee charged for
ticket types with ``service_fee_included`` set, previously a stored-but-
inert flag.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-27

Per-event (like every other EventConfig money field) rather than global, so
different events can charge different fees or none at all. Defaults to
0.00, so an existing event's checkout total is unaffected until an admin
sets one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_configs",
        sa.Column("service_fee_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
    )
    op.alter_column("event_configs", "service_fee_amount", server_default=None)


def downgrade() -> None:
    op.drop_column("event_configs", "service_fee_amount")
