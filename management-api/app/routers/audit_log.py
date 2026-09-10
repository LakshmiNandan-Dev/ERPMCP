from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.db import audit_engine
from app.deps import admin_subject
from app.models.audit import audit_log
from app.schemas.audit import AuditLogEntryOut

router = APIRouter(prefix="/audit-log", tags=["audit-log"])


@router.get("", response_model=list[AuditLogEntryOut])
def list_audit_log(
    subject: str | None = None,
    environment: str | None = None,
    tool_name: str | None = None,
    instance: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, le=500),
    _caller: str = Depends(admin_subject),
) -> list[AuditLogEntryOut]:
    query = select(audit_log)
    if subject:
        # Substring, case-insensitive: an operator investigating an incident
        # has a name or a fragment of one, not the exact stored casing of an
        # email — an equality match here made the field almost unusable.
        query = query.where(audit_log.c.subject.ilike(f"%{subject}%"))
    if environment:
        query = query.where(audit_log.c.environment == environment)
    if tool_name:
        query = query.where(audit_log.c.tool_name.ilike(f"%{tool_name}%"))
    if instance:
        query = query.where(audit_log.c.instance == instance.upper())
    if status:
        query = query.where(audit_log.c.status == status)
    if since:
        query = query.where(audit_log.c.occurred_at >= since)
    if until:
        # Inclusive: the caller sends an end-of-day timestamp for a date
        # picked in the console, so <= is what they mean by "to this date".
        query = query.where(audit_log.c.occurred_at <= until)

    query = query.order_by(audit_log.c.occurred_at.desc()).limit(limit)

    # audit_engine connects as mgmt_audit_reader, a role granted only
    # ebsmcp_audit_reader — this endpoint could not write to the audit log
    # even if a bug tried to; the database enforces it, not this function.
    with audit_engine.connect() as conn:
        rows = conn.execute(query).mappings().all()
        return [AuditLogEntryOut.model_validate(row) for row in rows]
