"""add domain column to identity mappings

Revision ID: 7b852db3e538
Revises: ed6dd7f4da57
Create Date: 2026-08-31 23:12:05.724503

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b852db3e538'
down_revision: Union[str, Sequence[str], None] = 'ed6dd7f4da57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


identity_mappings = sa.table(
    "identity_mappings",
    sa.column("domain", sa.String),
    sa.column("target_system", sa.String),
)


def upgrade() -> None:
    """Upgrade schema."""
    # Added nullable first, backfilled, then gated by a CHECK constraint —
    # same "row already seeded in this database" problem as
    # target_username in 668ac12483c1, and the same fix. 'unclassified' is
    # a placeholder, not a real pillar derived from FND_APPLICATION —
    # existing ebs/fusion mappings need re-onboarding through the
    # admin-gui to pick up a real domain.
    op.add_column('identity_mappings', sa.Column('domain', sa.String(length=40), nullable=True))
    op.execute(
        identity_mappings.update()
        .where(identity_mappings.c.target_system != "ebs_dba")
        .values(domain="unclassified")
    )
    op.create_check_constraint(
        'ck_identity_mappings_domain_required_unless_dba',
        'identity_mappings',
        "target_system = 'ebs_dba' OR domain IS NOT NULL",
    )

    # The open-ended-mapping uniqueness index now includes domain, via
    # coalesce() to a sentinel so NULL-domain (ebs_dba) rows still collide
    # with each other correctly — verified directly against this database
    # before writing this migration (see models.py's comment on the
    # index). No autogenerate false positive to strip here: this database
    # only ever reflects the Postgres-dialect index, so the Oracle
    # function-based-index branch never shows up in the diff.
    op.drop_index(
        op.f('ux_identity_mappings_open_ended'),
        table_name='identity_mappings',
        postgresql_where='(effective_end_date IS NULL)',
    )
    op.create_index(
        'ux_identity_mappings_open_ended',
        'identity_mappings',
        ['entra_subject', 'environment', 'target_system', sa.literal_column("coalesce(domain, '')")],
        unique=True,
        postgresql_where=sa.text('effective_end_date IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ux_identity_mappings_open_ended',
        table_name='identity_mappings',
        postgresql_where=sa.text('effective_end_date IS NULL'),
    )
    op.create_index(
        op.f('ux_identity_mappings_open_ended'),
        'identity_mappings',
        ['entra_subject', 'environment', 'target_system'],
        unique=True,
        postgresql_where='(effective_end_date IS NULL)',
    )
    op.drop_constraint('ck_identity_mappings_domain_required_unless_dba', 'identity_mappings', type_='check')
    op.drop_column('identity_mappings', 'domain')
