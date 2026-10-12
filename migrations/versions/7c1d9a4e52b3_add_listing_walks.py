"""Add the walks from each listing to bus stops

Revision ID: 7c1d9a4e52b3
Revises: b8e41d7c2a95
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1d9a4e52b3"
down_revision: str | Sequence[str] | None = "b8e41d7c2a95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Filled in by the app, for the bus lines users choose (see walking.py)
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("walks", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_column("walks")
