"""Add orders.mollie_mode (pinned at payment-creation time).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-08

Milestone 3's security review found that the Mollie webhook re-reading
``EventConfig.mollie_mode`` live (rather than the mode that was actually
active when a given Order's payment was created) meant an admin flipping
test/live while an Order was still ``pending`` would make reconciliation
fetch with the wrong-environment key and get stuck forever (Mollie
rejects the mismatched key, the webhook 502s, Mollie retries indefinitely
against a key that will never work). This column snapshots the mode at
payment-creation time — see ``app.models.order.Order.mollie_mode``'s
docstring — so reconciliation is immune to a mid-flight config change,
the same way ``Order.total``/``Ticket.price`` are already snapshots
rather than live-recomputed values.

Reuses the existing ``mollie_mode`` native enum type (created in 0006 for
``event_configs.mollie_mode``) rather than creating a second one —
``create_type=False`` on the column definition, only ``drop_type=False``
on downgrade's column drop (the type itself is still owned/dropped by
0006's downgrade, not this migration).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_mollie_mode_enum = postgresql.ENUM("test", "live", name="mollie_mode", create_type=False)


def upgrade() -> None:
    op.add_column("orders", sa.Column("mollie_mode", _mollie_mode_enum, nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "mollie_mode")
