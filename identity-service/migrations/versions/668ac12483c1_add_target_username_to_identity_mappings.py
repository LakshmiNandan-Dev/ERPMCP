"""add target_username to identity_mappings

Revision ID: 668ac12483c1
Revises: 8e8baf37c76a
Create Date: 2026-08-31 21:56:19.911042

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '668ac12483c1'
down_revision: Union[str, Sequence[str], None] = '8e8baf37c76a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


identity_mappings = sa.table(
    "identity_mappings", sa.column("target_username", sa.String), sa.column("entra_subject", sa.String)
)


def upgrade() -> None:
    """Upgrade schema."""
    # Note: the spurious identity_mappings-index drop/recreate that
    # autogenerate proposes here (same false positive as the
    # entra_registrations migration — see that file's comment) is removed.
    #
    # Added nullable first, backfilled, then made NOT NULL — a straight
    # NOT NULL add would fail against the row already seeded in this
    # database. The backfill value (entra_subject) is a placeholder, not a
    # real EBS/Fusion username; existing mappings need re-onboarding
    # through the admin-gui to get a real target_username.
    op.add_column("identity_mappings", sa.Column("target_username", sa.String(length=100), nullable=True))
    op.execute(identity_mappings.update().values(target_username=identity_mappings.c.entra_subject))
    op.alter_column("identity_mappings", "target_username", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("identity_mappings", "target_username")
