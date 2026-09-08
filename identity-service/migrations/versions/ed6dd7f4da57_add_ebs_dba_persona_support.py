"""add ebs_dba persona support

Revision ID: ed6dd7f4da57
Revises: 668ac12483c1
Create Date: 2026-08-31 22:42:00.554812

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed6dd7f4da57'
down_revision: Union[str, Sequence[str], None] = '668ac12483c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Hand-corrected after autogenerate — same recurring index false
    # positive as every prior migration (removed, see those files'
    # comments), plus one thing autogenerate genuinely missed: it compares
    # CHECK constraints by name, so a same-named constraint whose
    # definition changed (ck_identity_mappings_target_system needs
    # 'ebs_dba' added to its allowed list) is invisible to it. Silently
    # trusting autogenerate here would have shipped a migration that adds
    # ebs_dba everywhere except the one constraint that actually
    # gatekeeps it — insert attempts would fail the CHECK before the
    # application ever got a chance to explain why.
    op.drop_constraint("ck_identity_mappings_target_system", "identity_mappings", type_="check")
    op.create_check_constraint(
        "ck_identity_mappings_target_system",
        "identity_mappings",
        "target_system IN ('ebs', 'fusion', 'ebs_dba')",
    )
    op.alter_column('identity_mappings', 'target_username',
               existing_type=sa.VARCHAR(length=100),
               nullable=True)
    op.create_check_constraint('ck_identity_mappings_username_required_unless_dba', 'identity_mappings', "target_system = 'ebs_dba' OR target_username IS NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_identity_mappings_username_required_unless_dba', 'identity_mappings', type_='check')
    op.alter_column('identity_mappings', 'target_username',
               existing_type=sa.VARCHAR(length=100),
               nullable=False)
    op.drop_constraint("ck_identity_mappings_target_system", "identity_mappings", type_="check")
    op.create_check_constraint(
        "ck_identity_mappings_target_system",
        "identity_mappings",
        "target_system IN ('ebs', 'fusion')",
    )
