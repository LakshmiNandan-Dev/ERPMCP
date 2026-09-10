"""Audit logging: "Record" in the request flow.

Every request and response gets recorded — including denials and errors,
not just successes, since "an entitlement check rejected this call" is
exactly the kind of event an audit trail exists to capture.

This writes structured JSON lines to stdout: stdout from a container is
what any enterprise log pipeline (Fluent Bit, Vector, whatever the platform
already runs) picks up first. The append-only store in audit-service is the
second sink on the same AuditRecord shape, added here rather than replacing
stdout, and enabled by passing db_url (AUDIT_DB_URL).

Both sinks matter and they fail independently. stdout is the one that must
not fail, so the database write is best-effort: if it raises, the failure is
reported on stdout and the tool call still succeeds. The record is already
durable in the log pipeline by then, and an audit-store outage must not
become an outage of the product — refusing every tool call because a
reporting database is down trades a real capability for no extra safety.
The reverse is not true, which is why stdout is written first: an audit
record that exists only in a database nobody has queried yet is weaker than
one already in the log stream.
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

from sqlalchemy import Engine, create_engine, insert

from ebsmcp.audit.tables import audit_log


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
    def __init__(self, db_url: str | None = None, engine: Engine | None = None) -> None:
        """db_url unset (and no engine injected) keeps stdout as the only
        sink — the documented fallback, not a broken state. engine is for
        tests, same injection point as PostgresIdentityResolver.
        """
        self._engine: Engine | None = engine
        if self._engine is None and db_url:
            self._engine = create_engine(db_url, pool_pre_ping=True)

    def _emit(self, record: AuditRecord) -> None:
        # stdout first, unconditionally: see the module docstring on why this
        # is the sink that must not fail.
        print(json.dumps(asdict(record)), file=sys.stdout, flush=True)
        if self._engine is not None:
            self._write_to_store(record)

    def _write_to_store(self, record: AuditRecord) -> None:
        """Best-effort insert into audit-service's audit_log."""
        try:
            with self._engine.begin() as conn:  # type: ignore[union-attr]
                conn.execute(
                    insert(audit_log).values(
                        correlation_id=record.correlation_id,
                        # The record's own timestamp, not the insert time:
                        # occurred_at is the partition key, so a row must land
                        # in the month the call actually happened in.
                        occurred_at=datetime.fromisoformat(record.timestamp),
                        tool_name=record.tool_name,
                        subject=record.subject,
                        environment=record.environment,
                        target_system=record.target_system,
                        status=record.status,
                        # Which EBS database the call was routed to. None is
                        # a real answer for tools that touch no instance
                        # (list_ebs_instances), not missing data.
                        instance=record.instance,
                        # tuple -> list so it is JSON-encodable on both sinks.
                        effective_org_ids=list(record.effective_org_ids),
                        params=record.params,
                        error_message=record.error_message,
                        latency_ms=record.latency_ms,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - see module docstring
            print(
                json.dumps(
                    {
                        "audit_sink_error": str(exc),
                        "correlation_id": record.correlation_id,
                        "tool_name": record.tool_name,
                    }
                ),
                file=sys.stdout,
                flush=True,
            )

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
