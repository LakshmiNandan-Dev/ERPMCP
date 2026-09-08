"""Exercises instance_status's real outcomes through an actual
ClientSession over the in-memory transport — same rationale as
test_example_health_tool.py: a real MCP client only ever sees a
CallToolResult with is_error set, never a raised exception.

ToolContext is built directly here rather than through Settings/build_app,
same reasoning as test_example_health_tool.py — it lets each test give a
subject exactly the mappings it needs, including the persona-boundary
case (a subject with only a functional mapping, no ebs_dba one).
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
from ebsmcp.tools import INSTANCE_HEALTH_TOOLSET, ToolContext, mount_toolsets

DBA_SUBJECT = "dba@corp.com"
FUNCTIONAL_ONLY_SUBJECT = "jdoe@corp.com"
UNMAPPED_SUBJECT = "someone-else@corp.com"

CANNED_INSTANCES = [
    {"instance_name": "TESTDB1", "status": "OPEN", "database_status": "ACTIVE",
     "host_name": "testhost1", "version": "19.0.0.0.0", "startup_time": "2026-08-01T03:00:00"},
    {"instance_name": "TESTDB2", "status": "OPEN", "database_status": "ACTIVE",
     "host_name": "testhost2", "version": "19.0.0.0.0", "startup_time": "2026-08-01T03:00:00"},
]


def build_test_app(dev_subject: str) -> MCPServer:
    app = MCPServer("ebsmcp-test")
    ctx = ToolContext(
        connectors={"TEST": MockEBSConnector(canned_responses={"gv$instance": CANNED_INSTANCES})},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject=DBA_SUBJECT,
                environment="test",
                target_system="ebs_dba",
                mapped_role="Senior DBA — patching access",
                allowed_org_ids=(),
            ),
            ResolvedIdentity(
                subject=FUNCTIONAL_ONLY_SUBJECT,
                environment="test",
                target_system="ebs",
                mapped_role="AP_MANAGER",
                allowed_org_ids=("204",),
            ),
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject=dev_subject,
    )
    mount_toolsets(app, ctx, [INSTANCE_HEALTH_TOOLSET])
    return app


async def call_instance_status(app: MCPServer):
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("instance_status", {})


@pytest.mark.asyncio
async def test_dba_mapped_caller_gets_every_instance_row():
    app = build_test_app(dev_subject=DBA_SUBJECT)
    result = await call_instance_status(app)

    assert not result.is_error
    text = result.content[0].text
    assert "TESTDB1" in text
    assert "TESTDB2" in text
    assert "Senior DBA" in text


@pytest.mark.asyncio
async def test_unmapped_caller_gets_is_error_with_the_real_reason():
    app = build_test_app(dev_subject=UNMAPPED_SUBJECT)
    result = await call_instance_status(app)

    assert result.is_error
    assert "No identity mapping" in result.content[0].text


@pytest.mark.asyncio
async def test_functional_only_caller_is_denied_the_dba_toolset():
    """The persona boundary: holding a valid ebs mapping does not grant
    ebs_dba access — a caller with no open ebs_dba mapping must be denied
    exactly like an unmapped caller, not silently handed DBA-scoped data
    for a different persona's identity."""
    app = build_test_app(dev_subject=FUNCTIONAL_ONLY_SUBJECT)
    result = await call_instance_status(app)

    assert result.is_error
    assert "No identity mapping" in result.content[0].text
