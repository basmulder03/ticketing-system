"""Add 'manual' to the payment_method enum.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-10

Post-launch fix, per the user's NOTES: "For people without a computer or
phone, allow for an admin to create/do things with tickets, like creating
them for a show, e.g. without having the payment process (of course with
the correct audit logging)." Unlike ``mollie``/``door``/``demo``, this
value is never buyer-selectable at checkout (never added to
``EventConfig.enabled_payment_methods``) — it marks an Order an admin
created directly for a walk-up buyer who never submitted any checkout
request at all. See ``app.services.manual_order.create_manual_order`` and
``app.models.enums.PaymentMethod``'s docstring.

``ALTER TYPE ... ADD VALUE`` runs in an autocommit block — see
alembic/versions/0007_actor_type_system.py's migration docstring for why.
Downgrading removes the value by recreating the enum type without it, the
same way 0011 did for 'demo'.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("mollie", "door", "demo")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'manual'")


def downgrade() -> None:
    # No live Order/EventConfig row should reference payment_method=manual
    # past this point — same reasoning and same mechanics as 0011's
    # downgrade for 'demo'.
    op.execute(
        "DELETE FROM tickets WHERE order_id IN (SELECT id FROM orders WHERE payment_method = 'manual')"
    )
    op.execute("DELETE FROM orders WHERE payment_method = 'manual'")
    op.execute(
        "UPDATE event_configs SET enabled_payment_methods = array_remove(enabled_payment_methods, 'manual')"
    )

    new_enum_tmp = sa.Enum(*_OLD_VALUES, name="payment_method_new")
    new_enum_tmp.create(op.get_bind(), checkfirst=True)
    op.execute(
        "ALTER TABLE orders "
        "ALTER COLUMN payment_method TYPE payment_method_new "
        "USING payment_method::text::payment_method_new"
    )
    op.execute(
        "ALTER TABLE event_configs "
        "ALTER COLUMN enabled_payment_methods TYPE payment_method_new[] "
        "USING enabled_payment_methods::text[]::payment_method_new[]"
    )
    op.execute("DROP TYPE payment_method")
    op.execute("ALTER TYPE payment_method_new RENAME TO payment_method")
