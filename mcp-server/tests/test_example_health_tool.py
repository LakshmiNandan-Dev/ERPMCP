"""Exercises server_health's three real outcomes through an actual
ClientSession over the in-memory transport — not the server's internal
call_tool() shortcut, which raises exceptions directly and doesn't reflect
what a real MCP client (Copilot, an agent) experiences. A real client only
ever sees a CallToolResult with is_error set, never a raised exception; the
tests below confirm that's genuinely what happens on the wire, not just
what our own code intends.

ToolContext is built directly here rather than through Settings/build_app —
the env-var-driven wiring in server.py ties "the caller" and "the one fixed
mapping" to the same value by construction, which can't exercise an
unmapped-caller scenario.
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
from ebsmcp.tools import HEALTH_TOOLSET, ToolContext, mount_toolsets

MAPPED_SUBJECT = "jdoe@corp.com"
UNMAPPED_SUBJECT = "someone-else@corp.com"


def build_test_app(dev_subject: str, allowed_org_ids: tuple[str, ...] = ("204",)) -> MCPServer:
    app = MCPServer("ebsmcp-test")
    ctx = ToolContext(
        connectors={
            "TEST": MockEBSConnector(
                canned_responses={
                    "gv$instance": [
                        {"instance_name": "TESTDB1", "status": "OPEN", "database_status": "ACTIVE"}
                    ]
                }
            )
        },
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject=MAPPED_SUBJECT,
                environment="test",
                target_system="ebs",
                mapped_role="AP_MANAGER",
                allowed_org_ids=allowed_org_ids,
            )
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject=dev_subject,
    )
    mount_toolsets(app, ctx, [HEALTH_TOOLSET])
    return app


async def call_server_health(app: MCPServer):
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("server_health", {})


@pytest.mark.asyncio
async def test_mapped_caller_gets_a_successful_result():
    app = build_test_app(dev_subject=MAPPED_SUBJECT)
    result = await call_server_health(app)

    assert not result.is_error
    assert '"effective_org_ids": [\n    "204"\n  ]' in result.content[0].text


@pytest.mark.asyncio
async def test_unmapped_caller_gets_is_error_with_the_real_reason():
    """The core protocol-level guarantee: a real MCP client sees is_error
    set, with the actual "no identity mapping" message, not a normal-looking
    result it would have to know to inspect for an app-specific status field.
    """
    app = build_test_app(dev_subject=UNMAPPED_SUBJECT)
    result = await call_server_health(app)

    assert result.is_error
    assert "No identity mapping" in result.content[0].text


@pytest.mark.asyncio
async def test_mapped_caller_with_no_allowed_orgs_gets_is_error():
    app = build_test_app(dev_subject=MAPPED_SUBJECT, allowed_org_ids=())
    result = await call_server_health(app)

    assert result.is_error
    assert "no permitted Org ID overlap" in result.content[0].text
