"""add HR person scope and sensitive field grants

Revision ID: d5a3e9c71b48
Revises: c8f2a41b6d70
Create Date: 2026-10-14 00:00:00.000000

Org IDs say which operating unit a caller may see, not which people. HCM
data needs the second thing: ebs_person_id is the caller's own person,
hr_scope is how far from it they may look (self / reports / all), and
identity_mapping_sensitive_grants is the explicit list of sensitive field
classes (compensation, national ID, bank account, ...) they may see on
records that are not their own. Absence of a grant is denial.

Existing rows get hr_scope='reports' and no grants and no person id, so an
existing hcm mapping loads but is denied until re-onboarded with a person —
fail-closed, by design.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5a3e9c71b48'
down_revision: Union[str, Sequence[str], None] = 'c8f2a41b6d70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FIELD_CLASSES = (
    "compensation", "national_id", "date_of_birth", "bank_account",
    "tax_id", "medical", "home_contact",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('identity_mappings', sa.Column('ebs_person_id', sa.Integer(), nullable=True))
    op.add_column(
        'identity_mappings',
        sa.Column('hr_scope', sa.String(length=10), nullable=False, server_default='reports'),
    )
    op.create_check_constraint(
        'ck_identity_mappings_hr_scope',
        'identity_mappings',
        "hr_scope IN ('self', 'reports', 'all')",
    )
    op.create_table(
        'identity_mapping_sensitive_grants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('identity_mapping_id', sa.Integer(), nullable=False),
        sa.Column('field_class', sa.String(length=40), nullable=False),
        sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('granted_by', sa.String(length=320), nullable=False),
        sa.CheckConstraint(
            f"field_class IN {_FIELD_CLASSES}",
            name='ck_mapping_sensitive_grants_field_class',
        ),
        sa.ForeignKeyConstraint(['identity_mapping_id'], ['identity_mappings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'identity_mapping_id', 'field_class', name='ux_mapping_sensitive_grants_no_dupes'
        ),
    )

    # management-api creates grants as part of creating a mapping, so its
    # runtime role needs to read and insert them — and nothing more: no
    # UPDATE or DELETE, so a grant is added when a mapping is created and
    # ends when the mapping is closed, never edited in place. Guarded like
    # the admin_accounts grant: that role is not provisioned by any tracked
    # migration. The role mcp-server reads with is provisioned outside this
    # repo and needs SELECT on this table granted the same way.
    bind = op.get_bind()
    if bind.dialect.name != "oracle":
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mgmt_identity_writer') THEN
                    GRANT SELECT, INSERT ON identity_mapping_sensitive_grants
                        TO mgmt_identity_writer;
                    GRANT USAGE ON SEQUENCE identity_mapping_sensitive_grants_id_seq
                        TO mgmt_identity_writer;
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('identity_mapping_sensitive_grants')
    op.drop_constraint('ck_identity_mappings_hr_scope', 'identity_mappings', type_='check')
    op.drop_column('identity_mappings', 'hr_scope')
    op.drop_column('identity_mappings', 'ebs_person_id')
