"""Branch-level tests for diagnose_request's actual logic (not_running
early-return, session-not-found, blocker vs. no-blocker causes,
unknown-request ToolError) — the parts of this composed tool that are
real business logic, not just a query. Full-protocol proof via a real
ClientSession, same as every other DBA tool test.

Each branch needs its own MockEBSConnector, not just different
request_id values passed to the same app: diagnose_request's request
lookup binds :request_id rather than interpolating it, so the SQL text
(what the mock matches on) is identical regardless of which request_id
is asked for — the mock can only vary its canned response per app
instance, not per call.
"""

from __future__ import annotations

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors import MockEBSConnector
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import ToolContext, mount_toolsets
from ebsmcp.tools.dba import DIAGNOSE_REQUEST_TOOLSET

DBA_SUBJECT = "dba@corp.com"

_BASE_REQUEST_ROW = {
    "request_id": 999,
    "phase_code": "R",
    "status_code": "R",
    "actual_start_date": "2026-09-01T02:00:00",
    "elapsed_minutes": 45.0,
    "program_name": "Create Accounting",
    "program_short_name": "CREATE_ACCOUNTING",
    "os_process_id": "30105",
    "concurrent_program_id": 20450,
    "program_application_id": 101,
}

_BASE_SESSION_ROW = {
    "sid": 812,
    "serial#": 4491,
    "inst_id": 1,
    "status": "ACTIVE",
    "event": "db file sequential read",
    "wait_class": "User I/O",
    "blocking_session": None,
    "sql_id": None,
    "module": "CREATE_ACCOUNTING",
}


def build_test_app(canned_responses: dict) -> MCPServer:
    app = MCPServer("ebsmcp-test")
    ctx = ToolContext(
        connectors={"TEST": MockEBSConnector(canned_responses=canned_responses)},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject=DBA_SUBJECT,
                environment="test",
                target_system="ebs_dba",
                mapped_role="Senior DBA — patching access",
                allowed_org_ids=(),
            )
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject=DBA_SUBJECT,
    )
    mount_toolsets(app, ctx, [DIAGNOSE_REQUEST_TOOLSET])
    return app


async def call_diagnose_request(app: MCPServer, request_id: int = 999):
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("diagnose_request", {"request_id": request_id})


@pytest.mark.asyncio
async def test_unknown_request_id_raises_tool_error():
    app = build_test_app({"elapsed_minutes": []})
    result = await call_diagnose_request(app)
    assert result.is_error
    assert "No concurrent request found" in result.content[0].text


@pytest.mark.asyncio
async def test_not_running_returns_scheduling_diagnosis_without_querying_session():
    app = build_test_app({"elapsed_minutes": [{**_BASE_REQUEST_ROW, "phase_code": "P"}]})
    result = await call_diagnose_request(app)
    assert not result.is_error
    assert '"diagnosis": "not_running"' in result.content[0].text


@pytest.mark.asyncio
async def test_session_not_found_when_running_but_no_gv_session_match():
    app = build_test_app({"elapsed_minutes": [_BASE_REQUEST_ROW], "gv$session gs": []})
    result = await call_diagnose_request(app)
    assert not result.is_error
    assert '"diagnosis": "session_not_found"' in result.content[0].text


@pytest.mark.asyncio
async def test_blocked_session_surfaces_blocker_as_likely_cause():
    app = build_test_app(
        {
            "elapsed_minutes": [_BASE_REQUEST_ROW],
            "gv$session gs": [{**_BASE_SESSION_ROW, "blocking_session": 555}],
            "gv$session_longops": [],
            "avg_minutes": [{"avg_minutes": 100.0, "median_minutes": 90.0}],
        }
    )
    result = await call_diagnose_request(app)
    assert not result.is_error
    assert "Blocked by session 555" in result.content[0].text


@pytest.mark.asyncio
async def test_no_blocker_but_running_long_surfaces_historical_comparison():
    """elapsed_minutes=45 vs. avg_minutes=10 — more than double, should
    surface as a likely cause even with no blocker present."""
    app = build_test_app(
        {
            "elapsed_minutes": [_BASE_REQUEST_ROW],
            "gv$session gs": [_BASE_SESSION_ROW],
            "gv$session_longops": [],
            "avg_minutes": [{"avg_minutes": 10.0, "median_minutes": 8.5}],
        }
    )
    result = await call_diagnose_request(app)
    assert not result.is_error
    text = result.content[0].text
    assert "historical average" in text
    assert "Blocked by session" not in text


@pytest.mark.asyncio
async def test_no_blocker_and_within_norms_reports_no_cause_found():
    app = build_test_app(
        {
            "elapsed_minutes": [_BASE_REQUEST_ROW],
            "gv$session gs": [_BASE_SESSION_ROW],
            "gv$session_longops": [],
            "avg_minutes": [{"avg_minutes": 40.0, "median_minutes": 38.0}],
        }
    )
    result = await call_diagnose_request(app)
    assert not result.is_error
    assert "within historical norms" in result.content[0].text
