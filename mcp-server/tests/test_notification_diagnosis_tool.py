"""Branch-level tests for notification_diagnosis's actual logic
(not-found, activity-error cause, mailer-down cause, no-cause-found) —
same rationale and shape as test_diagnose_request_tool.py: each branch
needs its own MockEBSConnector since the underlying queries bind
notification_id rather than interpolating it, so the SQL text is
identical regardless of which notification_id is asked for.
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
from ebsmcp.tools.dba import NOTIFICATION_DIAGNOSIS_TOOLSET

DBA_SUBJECT = "dba@corp.com"

_BASE_NOTIFICATION_ROW = {
    "notification_id": 88231,
    "message_type": "POAPPRV",
    "message_name": "APPROVE_REQ",
    "subject": "PO Approval Required",
    "status": "OPEN",
    "mail_status": "MAILED",
    "recipient_role": "SYSADMIN",
    "begin_date": "2026-08-31T10:00:00",
    "end_date": None,
    "responder": None,
}

_HEALTHY_ACTIVITY_ROW = {
    "item_type": "POAPPRV",
    "item_key": "3010master882",
    "activity_status": "ACTIVE",
    "assigned_user": "SYSADMIN",
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
    mount_toolsets(app, ctx, [NOTIFICATION_DIAGNOSIS_TOOLSET])
    return app


async def call_notification_diagnosis(app: MCPServer, notification_id: int = 88231):
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("notification_diagnosis", {"notification_id": notification_id})


@pytest.mark.asyncio
async def test_unknown_notification_id_raises_tool_error():
    app = build_test_app({"wn.notification_id = :notification_id": []})
    result = await call_notification_diagnosis(app)
    assert result.is_error
    assert "No notification found" in result.content[0].text


@pytest.mark.asyncio
async def test_errored_raising_activity_surfaces_as_likely_cause():
    app = build_test_app(
        {
            "wn.notification_id = :notification_id": [{**_BASE_NOTIFICATION_ROW, "mail_status": "OPEN"}],
            "wias.notification_id": [{**_HEALTHY_ACTIVITY_ROW, "activity_status": "ERROR"}],
        }
    )
    result = await call_notification_diagnosis(app)
    assert not result.is_error
    text = result.content[0].text
    assert "is in ERROR status" in text


@pytest.mark.asyncio
async def test_stuck_mail_status_with_downed_mailer_surfaces_as_likely_cause():
    app = build_test_app(
        {
            "wn.notification_id = :notification_id": [{**_BASE_NOTIFICATION_ROW, "mail_status": "FAILED"}],
            "wias.notification_id": [_HEALTHY_ACTIVITY_ROW],
            "component_status": [
                {"component_id": 301, "component_name": "Workflow Notification Mailer",
                 "component_status": "STOPPED", "last_update_date": "2026-09-01T05:00:00"},
            ],
        }
    )
    result = await call_notification_diagnosis(app)
    assert not result.is_error
    text = result.content[0].text
    assert "Mailer component(s) not running" in text
    assert "Workflow Notification Mailer" in text


@pytest.mark.asyncio
async def test_healthy_notification_and_activity_reports_no_cause_found():
    """mail_status="OPEN" (not stuck) means the mailer isn't even
    queried — only a genuinely stuck mail_status triggers that check."""
    app = build_test_app(
        {
            "wn.notification_id = :notification_id": [{**_BASE_NOTIFICATION_ROW, "mail_status": "OPEN"}],
            "wias.notification_id": [_HEALTHY_ACTIVITY_ROW],
        }
    )
    result = await call_notification_diagnosis(app)
    assert not result.is_error
    text = result.content[0].text
    assert "No obvious cause found" in text
    assert '"mailer_components": []' in text
