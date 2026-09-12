"""Table definitions matching identity-service's schema exactly.

This is a read/write *client* of that schema, not its owner: schema
changes happen only through identity-service's own Alembic migrations,
never through this module. It's a separate copy rather than a shared
import for the same reason PortableJSON is duplicated across
identity-service and audit-service — see those files — plus a second one
specific to this project's layout: identity-service and audit-service each
have a top-level package literally called `db`, which would collide if
this process tried to import both. Independent, small, stable table
definitions sidestep that entirely.

Keep this in sync with identity-service/db/models.py by hand when that
schema changes — there's no automation enforcing the two match, which is
the real cost of this duplication and worth knowing about.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects import oracle

metadata = MetaData()

TZDateTime = DateTime(timezone=True).with_variant(oracle.TIMESTAMP(timezone=True), "oracle")

identity_mappings = Table(
    "identity_mappings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("entra_subject", String(320), nullable=False),
    Column("environment", String(10), nullable=False),
    Column("target_system", String(10), nullable=False),
    Column("target_username", String(100), nullable=True),
    Column("domain", String(40), nullable=True),
    Column("mapped_role", String(240), nullable=False),
    Column("resolution_source", String(24), nullable=False),
    Column("effective_start_date", TZDateTime, nullable=False),
    Column("effective_end_date", TZDateTime, nullable=True),
    Column("created_at", TZDateTime, nullable=False),
    Column("created_by", String(320), nullable=False),
    Column("updated_at", TZDateTime, nullable=True),
    Column("updated_by", String(320), nullable=True),
)

identity_mapping_org_scope = Table(
    "identity_mapping_org_scope",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "identity_mapping_id",
        Integer,
        ForeignKey("identity_mappings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("org_id", String(64), nullable=False),
    Column("org_name", String(240), nullable=True),
    Column("resolved_from_source", Boolean, nullable=False),
)

entra_registrations = Table(
    "entra_registrations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("environment", String(10), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("audience", String(120), nullable=False),
    Column("subject_claim", String(60), nullable=False),
    Column("resource_server_url", String(500), nullable=False),
    Column("created_at", TZDateTime, nullable=False),
    Column("created_by", String(320), nullable=False),
    Column("updated_at", TZDateTime, nullable=True),
    Column("updated_by", String(320), nullable=True),
)

admin_accounts = Table(
    "admin_accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(320), nullable=False),
    Column("password_hash", String(100), nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", TZDateTime, nullable=False),
    Column("created_by", String(320), nullable=True),
    Column("updated_at", TZDateTime, nullable=True),
    Column("updated_by", String(320), nullable=True),
)


# Branding for this deployment's admin console — see identity-service's
# models.py for why it is a single row and why the logo is an inline data URI.
branding = Table(
    "branding",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("company_name", String(120), nullable=True),
    Column("logo_data_uri", Text, nullable=True),
    Column("updated_at", TZDateTime, nullable=True),
    Column("updated_by", String(320), nullable=True),
)
