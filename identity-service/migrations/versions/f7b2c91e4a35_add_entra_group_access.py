"""add Entra group access

Revision ID: f7b2c91e4a35
Revises: d5a3e9c71b48
Create Date: 2026-11-09 00:00:00.000000

Lets an administrator restrict who may use the tools to members of named
Entra / Active Directory groups, per environment and per channel (the chat
agent, or an MCP client with its own Entra token), optionally limited to some
domains.

entra_registrations gains enforce_group_access (default false, so every
existing environment behaves exactly as before) and groups_claim (default
"groups"). entra_group_access holds the grants. Existing rows are untouched:
nothing is enforced until an administrator turns it on, and the admin API
refuses to turn it on with no group granted, so a save cannot lock everyone out.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f7b2c91e4a35'
down_revision: Union[str, Sequence[str], None] = 'd5a3e9c71b48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENVIRONMENTS = ("dev", "test", "uat", "prod")
_CHANNELS = ("chat_agent", "mcp_direct", "any")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'entra_registrations',
        sa.Column('enforce_group_access', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'entra_registrations',
        sa.Column('groups_claim', sa.String(length=60), nullable=False, server_default='groups'),
    )
    op.create_table(
        'entra_group_access',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('environment', sa.String(length=10), nullable=False),
        sa.Column('group_object_id', sa.String(length=64), nullable=False),
        sa.Column('group_name', sa.String(length=200), nullable=False),
        sa.Column('channel', sa.String(length=12), nullable=False),
        sa.Column('allowed_domains', sa.String(length=200), nullable=False),
        sa.Column('effective_start_date', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('effective_end_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_by', sa.String(length=320), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_by', sa.String(length=320), nullable=True),
        sa.CheckConstraint(f"environment IN {_ENVIRONMENTS}", name='ck_entra_group_access_environment'),
        sa.CheckConstraint(f"channel IN {_CHANNELS}", name='ck_entra_group_access_channel'),
        sa.CheckConstraint(
            'effective_end_date IS NULL OR effective_end_date > effective_start_date',
            name='ck_entra_group_access_date_order',
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    bind = op.get_bind()
    if bind.dialect.name == "oracle":
        op.execute(
            "CREATE UNIQUE INDEX ux_entra_group_access_open ON entra_group_access "
            "((CASE WHEN effective_end_date IS NULL "
            "THEN environment || ':' || group_object_id || ':' || channel END))"
        )
    else:
        op.create_index(
            'ux_entra_group_access_open',
            'entra_group_access',
            ['environment', 'group_object_id', 'channel'],
            unique=True,
            postgresql_where=sa.text('effective_end_date IS NULL'),
        )
        # management-api adds, edits and closes grants; nothing deletes one.
        # Guarded like the other grants in this chain: those roles are not
        # provisioned by any tracked migration. The role mcp-server reads
        # with is provisioned outside this repo and needs SELECT on
        # entra_group_access and entra_registrations granted the same way.
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mgmt_identity_writer') THEN
                    GRANT SELECT, INSERT, UPDATE ON entra_group_access TO mgmt_identity_writer;
                    GRANT USAGE ON SEQUENCE entra_group_access_id_seq TO mgmt_identity_writer;
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('entra_group_access')
    op.drop_column('entra_registrations', 'groups_claim')
    op.drop_column('entra_registrations', 'enforce_group_access')
