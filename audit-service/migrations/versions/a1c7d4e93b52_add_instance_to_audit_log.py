"""add instance to audit_log

Revision ID: a1c7d4e93b52
Revises: 5426635c30f6
Create Date: 2026-09-10 16:30:00.000000

AuditRecord has always carried `instance` — which EBS database a call was
routed to — but audit_log had no column for it, so it reached stdout and
was dropped on the way to the store. On a deployment that reaches several
EBS instances, that made the audit trail unable to answer the question an
auditor is most likely to ask: which database did this person touch.

Nullable by necessity, not laziness: tools that touch no instance
(list_ebs_instances) genuinely have none, and every row written before this
migration has none either — backfilling a value would be inventing history.

Safe on the partitioned table: ADD COLUMN of a nullable column with no
default propagates to every partition without a rewrite.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1c7d4e93b52'
down_revision: Union[str, Sequence[str], None] = '5426635c30f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('audit_log', sa.Column('instance', sa.String(length=64), nullable=True))
    op.create_index('ix_audit_log_instance', 'audit_log', ['instance'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_audit_log_instance', table_name='audit_log')
    op.drop_column('audit_log', 'instance')
