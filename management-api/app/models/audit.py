"""Table definition matching audit-service's schema exactly. Read-only
from this API's perspective — see app/db.py, which connects to the audit
database using the ebsmcp_audit_reader role specifically, so "read-only"
is enforced at the database privilege level here too, not just by this
module never issuing an INSERT/UPDATE/DELETE.

Same duplication rationale as models/identity.py.
"""

from __future__ import annotations

import json

from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table
from sqlalchemy.dialects import oracle
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Text, TypeDecorator


class PortableJSON(TypeDecorator):
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


metadata = MetaData()

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
