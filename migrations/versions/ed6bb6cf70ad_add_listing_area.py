"""Add the official neighborhood of each listing

Revision ID: ed6bb6cf70ad
Revises: 32a1decc542f
Create Date: 2026-09-26 20:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed6bb6cf70ad"
down_revision: str | Sequence[str] | None = "32a1decc542f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Filled in by the app when it starts (see pipeline.refresh_areas)
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("area", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_listings_area"), ["area"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_listings_area"))
        batch_op.drop_column("area")
