"""Add 'system' to the actor_type enum.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-08

Milestone 3's security review flagged that automated payment-reconciliation
audit entries (Mollie webhook, preview-mode simulated payment — see
``app.services.order_payment.SYSTEM_PRINCIPAL``) were being written with
``actor_type=human``, which risks silent misclassification as staff
activity in any future audit-log view that segments actions by actor type.
This adds a real third ``ActorType.SYSTEM`` value (see
``app.models.enums.ActorType``'s docstring for the reasoning) rather than
continuing to overload ``human``.

``ALTER TYPE ... ADD VALUE`` runs in an autocommit block: PostgreSQL (12+)
allows it inside a transaction, but the new value can't be used within
that same transaction, and asyncpg's client-side enum/type-OID caching is
more reliable when this runs as its own committed statement rather than
folded into the same transaction as later migrations. Downgrading removes
the value by recreating the enum type without it — PostgreSQL has no
``DROP VALUE`` — which requires no live rows to still carry
``actor_type=system``, so downgrade() deletes any such AuditLogEntry rows
first (documented, not silent: this is a log table, not core business
data, and a downgrade past this revision is a dev/test operation, not
something expected against a live audit trail with real system entries
worth preserving).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("human", "ai_agent")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE actor_type ADD VALUE IF NOT EXISTS 'system'")


def downgrade() -> None:
    # No live audit entry should carry actor_type=system past this point —
    # see module docstring. Deleting (not erroring) keeps the standard
    # upgrade/downgrade/upgrade cycle this project's migrations are tested
    # with working unattended.
    op.execute("DELETE FROM audit_log_entries WHERE actor_type = 'system'")

    # Postgres has no ALTER TYPE ... DROP VALUE, so removing 'system'
    # means: create a differently-named enum with only the original two
    # values, repoint the column at it, drop the old (3-value) type, then
    # rename the new one back to "actor_type".
    new_enum_tmp = sa.Enum(*_OLD_VALUES, name="actor_type_new")
    new_enum_tmp.create(op.get_bind(), checkfirst=True)
    op.execute(
        "ALTER TABLE audit_log_entries "
        "ALTER COLUMN actor_type TYPE actor_type_new "
        "USING actor_type::text::actor_type_new"
    )
    op.execute("DROP TYPE actor_type")
    op.execute("ALTER TYPE actor_type_new RENAME TO actor_type")
