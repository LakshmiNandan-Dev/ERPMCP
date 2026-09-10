"""The audit store sink — the half of auditing that makes records queryable.

Every call was already emitted to stdout; what was missing was the insert
into audit-service's audit_log, which is what management-api reads and the
admin console's Audit log page shows. These tests pin that a record reaches
the table for every outcome, and that a failing store never takes a tool
call down with it.

SQLite in-memory stands in for Postgres, same rationale as
test_postgres_identity_resolver.py: the write is a plain single-row INSERT
with no dialect-specific construct. StaticPool for the same reason
conftest.py documents — one shared connection, so a write on one thread is
visible to a read on another.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from ebsmcp.audit import AuditLogger
from ebsmcp.audit.tables import audit_log, metadata


@pytest.fixture()
def audit_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(eng)
    return eng


@pytest.fixture()
def logger(audit_engine):
    return AuditLogger(engine=audit_engine)


def _rows(engine):
    with engine.connect() as conn:
        return conn.execute(select(audit_log)).mappings().all()


def test_successful_call_is_written_to_the_store(logger, audit_engine):
    with logger.audit_call(
        tool_name="instance_status", subject="jdoe@corp.com", environment="prod",
        target_system="ebs_dba", params={"instance": "PROD"},
    ) as outcome:
        outcome["effective_org_ids"] = ("204",)
        outcome["instance"] = "PROD"

    rows = _rows(audit_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool_name"] == "instance_status"
    assert row["subject"] == "jdoe@corp.com"
    assert row["status"] == "ok"
    assert row["environment"] == "prod"
    assert row["effective_org_ids"] == ["204"]
    assert row["params"] == {"instance": "PROD"}
    assert row["occurred_at"] is not None
    assert row["error_message"] is None


def test_instance_is_persisted(logger, audit_engine):
    """Which EBS database a call was routed to is the question a multi-
    instance deployment's audit trail most needs to answer, and it used to
    reach stdout and then be dropped on the way to the store."""
    with logger.audit_call(
        tool_name="tablespace_health", subject="jdoe@corp.com", environment="prod",
        target_system="ebs_dba", params={},
    ) as outcome:
        outcome["instance"] = "PROD"

    assert _rows(audit_engine)[0]["instance"] == "PROD"


def test_no_instance_is_recorded_as_null_not_invented(logger, audit_engine):
    """Tools that touch no instance (list_ebs_instances) genuinely have
    none — that must read as absent, not be back-filled with a guess."""
    with logger.audit_call(
        tool_name="list_ebs_instances", subject="jdoe@corp.com", environment="prod",
        target_system="ebs_dba", params={},
    ):
        pass

    assert _rows(audit_engine)[0]["instance"] is None


def test_denial_is_recorded_rather_than_lost(logger, audit_engine):
    """A rejected call is precisely what an audit trail exists to capture,
    so it must reach the store, not just the successful path."""
    from ebsmcp.policy import EntitlementDenied

    with pytest.raises(EntitlementDenied):
        with logger.audit_call(
            tool_name="fnd_user_status", subject="mallory@corp.com", environment="prod",
            target_system="ebs", params={},
        ):
            raise EntitlementDenied("requested org 999 is not permitted")

    rows = _rows(audit_engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "denied"
    assert "999" in rows[0]["error_message"]


def test_missing_mapping_is_distinguished_from_a_fault(logger, audit_engine):
    """no_identity_mapping is an onboarding gap, not a system error — the
    store has to preserve that distinction or the signal is lost."""
    with pytest.raises(RuntimeError):
        with logger.audit_call(
            tool_name="instance_status", subject="newhire@corp.com", environment="prod",
            target_system="ebs_dba", params={},
        ):
            raise RuntimeError("wrapped") from LookupError("no open mapping")

    assert _rows(audit_engine)[0]["status"] == "no_identity_mapping"


def test_every_outcome_lands_exactly_once(logger, audit_engine):
    for i in range(3):
        with logger.audit_call(
            tool_name=f"tool_{i}", subject="jdoe@corp.com", environment="prod",
            target_system="ebs_dba", params={},
        ):
            pass
    assert len(_rows(audit_engine)) == 3


def test_stdout_still_receives_the_record(logger, capsys):
    """The store is the second sink, not a replacement — a log pipeline must
    keep seeing every record."""
    with logger.audit_call(
        tool_name="instance_status", subject="jdoe@corp.com", environment="prod",
        target_system="ebs_dba", params={},
    ):
        pass
    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert emitted["tool_name"] == "instance_status"
    assert emitted["status"] == "ok"


def test_a_broken_store_does_not_break_the_tool_call(audit_engine, capsys):
    """An audit-database outage must not become a product outage. The record
    is already on stdout by then; the failure is reported there too rather
    than raised."""
    metadata.drop_all(audit_engine)  # table gone — every insert now fails
    logger = AuditLogger(engine=audit_engine)

    with logger.audit_call(
        tool_name="instance_status", subject="jdoe@corp.com", environment="prod",
        target_system="ebs_dba", params={},
    ):
        pass  # must not raise

    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert any(l.get("tool_name") == "instance_status" and l.get("status") == "ok" for l in lines)
    assert any("audit_sink_error" in l for l in lines), "the failure must be visible, not swallowed"


def test_no_store_configured_still_audits_to_stdout(capsys):
    """The documented fallback: AUDIT_DB_URL unset is a supported
    deployment, not a broken one."""
    AuditLogger()  # no db_url, no engine
    logger = AuditLogger()
    with logger.audit_call(
        tool_name="server_health", subject="jdoe@corp.com", environment="dev",
        target_system="ebs", params={},
    ):
        pass
    assert json.loads(capsys.readouterr().out.strip())["tool_name"] == "server_health"
