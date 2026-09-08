"""add instance scope to identity mappings

Revision ID: a17f9c4e6b2d
Revises: e6d741f37460
Create Date: 2026-09-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a17f9c4e6b2d'
down_revision: Union[str, Sequence[str], None] = 'e6d741f37460'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default hand-corrected after autogenerate, same recurring fix
    # as every other migration touching a Boolean column (see e.g.
    # e6d741f37460): the model declares false(), which needs to compile
    # per-dialect rather than as a raw Postgres-only `false` literal.
    # false is also the value every existing row gets — a mapping created
    # before this concept existed is "not instance-scoped", not
    # accidentally locked out of every EBS instance.
    op.add_column(
        'identity_mappings',
        sa.Column('instance_scope_restricted', sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        'identity_mapping_instance_scope',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('identity_mapping_id', sa.Integer(), nullable=False),
        sa.Column('instance_name', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['identity_mapping_id'], ['identity_mappings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'identity_mapping_id', 'instance_name', name='ux_mapping_instance_scope_no_dupes'
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('identity_mapping_instance_scope')
    op.drop_column('identity_mappings', 'instance_scope_restricted')
