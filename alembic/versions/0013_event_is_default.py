"""Add events.is_default_event, with a partial unique index enforcing at
most one.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-10

Post-launch fix, per the user's NOTES: "There should be a homepage on the
route, where different things can happen, and event can be set to the
default event, which causes that event page to automagically open to the
correct page." See ``app.web.routes.homepage`` and ``app.api.routes.
events``'s ``set_default_event``/``unset_default_event`` actions.

The partial unique index (``WHERE is_default_event``) is defense in depth
alongside the route-level "clear the previous default first, in the same
transaction" logic in ``set_default_event`` — it makes "more than one
default Event" a DB-level constraint violation, not just an application
bug that would otherwise fail silently (e.g. under a genuine race between
two concurrent admin requests).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("is_default_event", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("events", "is_default_event", server_default=None)
    op.create_index(
        "ix_events_is_default_event_true",
        "events",
        ["is_default_event"],
        unique=True,
        postgresql_where=sa.text("is_default_event"),
    )


def downgrade() -> None:
    op.drop_index("ix_events_is_default_event_true", table_name="events")
    op.drop_column("events", "is_default_event")
