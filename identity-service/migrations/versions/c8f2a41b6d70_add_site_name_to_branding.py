"""add site_name to branding

Revision ID: c8f2a41b6d70
Revises: b4d1e7a90c22
Create Date: 2026-09-12 00:00:00.000000

site_name is the console's own title — the white-label replacement for the
hardcoded "EBSMCP Admin" — as distinct from company_name, the organisation
that owns the deployment.

Nullable and unseeded, like the rest of this table: the header falls back
site_name -> company_name -> product default, so an existing row that set
only a company name keeps rendering exactly as it did before this column
existed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c8f2a41b6d70'
down_revision: Union[str, Sequence[str], None] = 'b4d1e7a90c22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('branding', sa.Column('site_name', sa.String(length=120), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('branding', 'site_name')
