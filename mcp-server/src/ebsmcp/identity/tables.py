"""Table shape matching identity-service's schema — only the columns
PostgresIdentityResolver actually reads. Independent copy, same rationale
as the duplication between identity-service, audit-service, and
management-api: this is a client of that schema, not its owner, and a
shared cross-package import isn't worth the coupling for a handful of
stable columns. Keep in sync by hand with identity-service/db/models.py.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, false
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
    Column("mapped_role", String(240), nullable=False),
    Column("effective_end_date", TZDateTime, nullable=True),
    Column("instance_scope_restricted", Boolean, nullable=False, server_default=false()),
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
    Column("resolved_from_source", Boolean, nullable=False),
)

# Only meaningful when the parent mapping's instance_scope_restricted is
# true — see identity_mappings' column above and ResolvedIdentity's
# allowed_instances docstring.
identity_mapping_instance_scope = Table(
    "identity_mapping_instance_scope",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "identity_mapping_id",
        Integer,
        ForeignKey("identity_mappings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("instance_name", String(64), nullable=False),
)
