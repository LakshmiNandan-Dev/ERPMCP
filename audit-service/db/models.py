"""Audit-log schema — the append-only record described throughout the
architecture doc: every request and response, success or failure, tagged
with a correlation ID.

This module defines the table structure only — columns, types, and the
constraints/indexes that differ by dialect. One-time provisioning DDL
(adding the partitioned table's primary key, creating initial partitions,
setting up append-only role grants) lives in db/provisioning.py instead,
wired into the Alembic migration directly via op.execute() — see that
module's docstring for why it isn't attached here as create-time event
hooks.

Postgres gets native monthly range partitioning (postgresql_partition_by
below), verified directly against a live instance before writing this for
real — a compliance audit table is exactly the kind where retrofitting
partitioning later, once it's live and growing, is genuinely painful, so
it's worth setting up correctly now rather than deferring it.

Oracle does NOT get partitioning here. Oracle Partitioning is a separately
licensed option on top of Enterprise Edition — the same kind of cost trap
as the Diagnostic/Tuning Pack calls made earlier in this project. If the
Oracle backend is chosen and audit volume eventually justifies it, that's a
deliberate licensing decision for later, not something to bake in by
default. A plain Oracle table works fine at the volumes this is likely to
see for a while.

status values are a plain VARCHAR + CHECK, not a native enum, on both
backends: Oracle has no native enum type at all (always emulated via
CHECK), and Postgres native enums are painful to extend later — a VARCHAR
CHECK is the one representation that's both portable and easy to widen when
a new status value is needed.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    func,
)
from sqlalchemy.dialects import oracle

from db.types import PortableJSON

metadata = MetaData()

# Same fix as identity-service/db/models.py: generic DateTime(timezone=True)
# silently compiles to plain Oracle DATE, dropping timezone information —
# verified directly, not assumed. See that file's comment for detail.
TZDateTime = DateTime(timezone=True).with_variant(oracle.TIMESTAMP(timezone=True), "oracle")

# Matches ebsmcp.audit.logger.AuditRecord.status exactly — see
# mcp-server/src/ebsmcp/audit/logger.py. If a new status is ever added
# there, this CHECK constraint (both dialects) needs the same value added
# in the same migration, or the audit write will fail outright rather than
# silently accept an unrecognized status.
VALID_STATUSES = ("ok", "denied", "no_identity_mapping", "error")

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, Identity(), nullable=False),
    Column("correlation_id", String(36), nullable=False),
    # func.now(), not text("now()") — the latter is a Postgres-only literal
    # (Oracle has no now() function at all) and was an early mistake here,
    # caught the same way as identity-service's server_default bug.
    Column("occurred_at", TZDateTime, nullable=False, server_default=func.now()),
    Column("tool_name", String(120), nullable=False),
    Column("subject", String(320), nullable=False),
    Column("environment", String(10), nullable=False),
    Column("target_system", String(10), nullable=False),
    Column("status", String(24), nullable=False),
    # Which EBS database the call was routed to. Nullable: tools that touch
    # no instance (list_ebs_instances) legitimately have none, and every row
    # written before this column existed has none either.
    Column("instance", String(64), nullable=True),
    Column("effective_org_ids", PortableJSON, nullable=True),
    Column("params", PortableJSON, nullable=True),
    Column("error_message", String(4000), nullable=True),
    Column("latency_ms", Float, nullable=False),
    CheckConstraint(f"status IN {VALID_STATUSES}", name="ck_audit_log_status"),
    # Postgres: a partitioned table's primary key must include the
    # partition key column, so it ends up being (id, occurred_at), not id
    # alone — added via provisioning.ADD_PRIMARY_KEY_POSTGRESQL rather than
    # declared here, since SQLAlchemy Core's PrimaryKeyConstraint and a
    # partitioned CREATE TABLE interact awkwardly together; create-then-
    # ALTER is the reliable order, verified directly.
    postgresql_partition_by="RANGE (occurred_at)",
)

Index("ix_audit_log_correlation_id", audit_log.c.correlation_id)
Index("ix_audit_log_subject_env", audit_log.c.subject, audit_log.c.environment)
# Filtered on directly by the admin console's Audit log page — same reason
# subject/environment above are indexed.
Index("ix_audit_log_instance", audit_log.c.instance)
