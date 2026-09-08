"""Audit logging: "Record" in the request flow.

Every request and response gets recorded — including denials and errors,
not just successes, since "an entitlement check rejected this call" is
exactly the kind of event an audit trail exists to capture.

This writes structured JSON lines to stdout for the core slice. That's
intentional, not a shortcut to fix later under a different design: stdout
from a container is what any enterprise log pipeline (Fluent Bit, Vector,
whatever the platform already runs) picks up first. The append-only
Postgres/Oracle audit store described in the architecture doc is a second
sink layered on top of this same AuditRecord shape, not a replacement for
it — swap or add to _emit() without changing any calling code.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterator


@dataclass
class AuditRecord:
    correlation_id: str
    tool_name: str
    subject: str
    environment: str
    target_system: str
    status: str  # "ok" | "denied" | "error"
    params: dict[str, Any] = field(default_factory=dict)
    effective_org_ids: tuple[str, ...] = ()
    instance: str | None = None
    error_message: str | None = None
    latency_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class AuditLogger:
    def _emit(self, record: AuditRecord) -> None:
        print(json.dumps(asdict(record)), file=sys.stdout, flush=True)

    @contextmanager
    def audit_call(
        self,
        *,
        tool_name: str,
        subject: str,
        environment: str,
        target_system: str,
        params: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """Wraps one tool call. Yields a mutable dict the caller fills in
        with effective_org_ids once the entitlement filter has run; always
        emits an AuditRecord on the way out, whether the call succeeded,
        was denied, or raised.
        """
        correlation_id = str(uuid.uuid4())
        started = time.monotonic()
        outcome: dict[str, Any] = {"effective_org_ids": (), "instance": None}

        try:
            yield outcome
        except Exception as exc:
            from ebsmcp.policy import EntitlementDenied

            # resolve_scoped_call re-raises a bare LookupError (no identity
            # mapping) as ToolError so the client sees a real message; that
            # rewrap sets __cause__ to the original LookupError, which is
            # what's checked here — an onboarding gap is a materially
            # different audit event than a genuine system fault, even
            # though both would otherwise collapse into the same "error".
            if isinstance(exc, EntitlementDenied):
                status = "denied"
            elif isinstance(exc.__cause__, LookupError):
                status = "no_identity_mapping"
            else:
                status = "error"
            self._emit(
                AuditRecord(
                    correlation_id=correlation_id,
                    tool_name=tool_name,
                    subject=subject,
                    environment=environment,
                    target_system=target_system,
                    status=status,
                    params=params,
                    effective_org_ids=outcome.get("effective_org_ids", ()),
                    instance=outcome.get("instance"),
                    error_message=str(exc),
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            )
            raise
        else:
            self._emit(
                AuditRecord(
                    correlation_id=correlation_id,
                    tool_name=tool_name,
                    subject=subject,
                    environment=environment,
                    target_system=target_system,
                    status="ok",
                    params=params,
                    effective_org_ids=outcome.get("effective_org_ids", ()),
                    instance=outcome.get("instance"),
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            )
