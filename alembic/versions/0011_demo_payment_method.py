"""Add 'demo' to the payment_method enum.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-10

Post-launch fix, per the user's NOTES: "create a custom [payment method],
that behaves something like mollie for a test environment/demo purposes
without having to do stuff with external applications." Adds a real,
selectable third ``PaymentMethod`` value (see
``app.models.enums.PaymentMethod``'s docstring) rather than a special-cased
dev-only backdoor — any event can enable it via the same
``EventConfig.enabled_payment_methods`` list Mollie/door already use.

``ALTER TYPE ... ADD VALUE`` runs in an autocommit block — see
alembic/versions/0007_actor_type_system.py's own migration docstring for
why (PostgreSQL allows this inside a transaction as of 12+, but the new
value can't be used within that same transaction, and asyncpg's
client-side enum/type-OID caching is more reliable when this runs as its
own committed statement). Downgrading removes the value by recreating the
enum type without it, the same way — no live Order should carry
payment_method=demo past a downgrade of this revision; documented, not
silent.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("mollie", "door")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'demo'")


def downgrade() -> None:
    # No live Order/EventConfig row should reference payment_method=demo
    # past this point. Orders: delete any such row's Tickets first (FK),
    # then the Order itself. EventConfig.enabled_payment_methods is an
    # ARRAY(payment_method) column -- strip 'demo' out of every array
    # rather than deleting the EventConfig row entirely.
    op.execute(
        "DELETE FROM tickets WHERE order_id IN (SELECT id FROM orders WHERE payment_method = 'demo')"
    )
    op.execute("DELETE FROM orders WHERE payment_method = 'demo'")
    op.execute(
        "UPDATE event_configs SET enabled_payment_methods = array_remove(enabled_payment_methods, 'demo')"
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
