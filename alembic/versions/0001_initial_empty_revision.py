"""Initial empty revision.

Revision ID: 0001
Revises:
Create Date: 2026-09-07

No models exist yet as of Milestone 0 — this establishes the migration
chain's root so ``backend-builder`` can add real revisions on top of it
starting Milestone 1.
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
