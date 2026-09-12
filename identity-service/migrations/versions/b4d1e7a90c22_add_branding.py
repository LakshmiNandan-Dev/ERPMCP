"""add branding

Revision ID: b4d1e7a90c22
Revises: a17f9c4e6b2d
Create Date: 2026-09-11 00:00:00.000000

Per-deployment admin-console branding. Single row, guarded by a CHECK rather
than by convention, because there is no per-environment dimension: one
deployment serves one customer.

Nullable throughout and no seeded row — an unconfigured deployment must render
the product default, and inserting a placeholder row here would make "never
configured" indistinguishable from "deliberately blank".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import oracle

revision: str = 'b4d1e7a90c22'
down_revision: Union[str, Sequence[str], None] = 'a17f9c4e6b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZDateTime = sa.DateTime(timezone=True).with_variant(oracle.TIMESTAMP(timezone=True), "oracle")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'branding',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_name', sa.String(length=120), nullable=True),
        sa.Column('logo_data_uri', sa.Text(), nullable=True),
        sa.Column('updated_at', TZDateTime, nullable=True),
        sa.Column('updated_by', sa.String(length=320), nullable=True),
        sa.CheckConstraint('id = 1', name='ck_branding_single_row'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('branding')
