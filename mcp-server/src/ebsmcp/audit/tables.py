"""Table shape matching audit-service's schema — only what AuditLogger
writes. Independent copy, same rationale as identity/tables.py and the
duplication between identity-service, audit-service and management-api:
this is a *client* of that schema, not its owner. Keep in sync by hand
with audit-service/db/models.py.

Deliberately omitted from this copy, because they belong to the owner:
the monthly RANGE partitioning, the (id, occurred_at) primary key, and the
status CHECK constraint. A writer doesn't declare them — it just has to
supply values the owner's constraints accept, and the status strings in
AuditRecord already match VALID_STATUSES there exactly.
"""

from __future__ import annotations

import json

from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table
from sqlalchemy.dialects import oracle
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Text, TypeDecorator

metadata = MetaData()


class PortableJSON(TypeDecorator):
    """JSONB on Postgres, JSON-encoded text everywhere else — same ~15-line
    type as audit-service/db/types.py, copied for the same reason."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.loads(value)


TZDateTime = DateTime(timezone=True).with_variant(oracle.TIMESTAMP(timezone=True), "oracle")

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("correlation_id", String(36), nullable=False),
    Column("occurred_at", TZDateTime, nullable=False),
    Column("tool_name", String(120), nullable=False),
    Column("subject", String(320), nullable=False),
    Column("environment", String(10), nullable=False),
    Column("target_system", String(10), nullable=False),
    Column("status", String(24), nullable=False),
    Column("effective_org_ids", PortableJSON, nullable=True),
    Column("params", PortableJSON, nullable=True),
    Column("error_message", String(4000), nullable=True),
    Column("latency_ms", Float, nullable=False),
)
