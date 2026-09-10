from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogEntryOut(BaseModel):
    id: int
    correlation_id: str
    occurred_at: datetime
    tool_name: str
    subject: str
    environment: str
    target_system: str
    status: str
    instance: str | None
    effective_org_ids: list[str] | None
    params: dict[str, Any] | None
    error_message: str | None
    latency_ms: float

    model_config = {"from_attributes": True}
