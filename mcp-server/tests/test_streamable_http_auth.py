"""The end-to-end proof that auth actually works over the wire, not just
that its pieces work in isolation: a real signed bearer token, sent as a
real HTTP Authorization header, through the real streamable-http ASGI app
(app.streamable_http_app()), verified by the real EntraTokenVerifier, with
the resulting subject flowing through get_access_token() into a real
PostgresIdentityResolver query (SQLite-backed) and the entitlement filter —
the entire chain from server.py, exercised together for the first time.

No real sockets: httpx2.ASGITransport drives the app in-process, and
StaticJWKSSource stands in for Entra's JWKS endpoint — the only two things
that would differ against a real tenant are the JWKS source and the
issuer/audience values, both externally configured, never hand-touched in
the code path this test exercises.
"""

from __future__ import annotations

import httpx2
import pytest
from asgi_lifespan import LifespanManager
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from ebsmcp.audit import AuditLogger
from ebsmcp.auth import EntraTokenVerifier, StaticJWKSSource
from ebsmcp.identity import PostgresIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import HEALTH_TOOLSET, ToolContext, mount_toolsets

RESOURCE_SERVER_URL = "http://127.0.0.1/"


def build_authed_app(test_jwks, test_issuer, test_audience, identity_engine) -> MCPServer:
    from ebsmcp.connectors import MockEBSConnector

    app = MCPServer(
        "ebsmcp-auth-test",
        token_verifier=EntraTokenVerifier(
            jwks_source=StaticJWKSSource(test_jwks),
            issuer=test_issuer,
            audience=test_audience,
        ),
        auth=AuthSettings(issuer_url=test_issuer, resource_server_url=RESOURCE_SERVER_URL),
    )

    ctx = ToolContext(
        connectors={
            "PROD": MockEBSConnector(
                canned_responses={"gv$instance": [{"instance_name": "AUTHTEST", "status": "OPEN", "database_status": "ACTIVE"}]}
            )
        },
        identity_resolver=PostgresIdentityResolver(db_url="unused", environment="prod", engine=identity_engine),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="prod",
        dev_subject="should-never-be-used@corp.com",
    )
    mount_toolsets(app, ctx, [HEALTH_TOOLSET])
    return app


async def call_over_real_http(app: MCPServer, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # allowed_hosts defaults to an empty list — DNS-rebinding protection
    # rejects everything until a host is explicitly allowed. A real
    # deployment sets this to its actual hostname; the test stands in
    # 127.0.0.1 for wherever RESOURCE_SERVER_URL points.
    asgi_app = app.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1"], allowed_origins=["http://127.0.0.1"]
        )
    )

    # StreamableHTTPSessionManager needs its own background task group
    # running, which a real ASGI server (uvicorn) starts via the ASGI
    # lifespan protocol automatically. httpx2.ASGITransport doesn't drive
    # lifespan events on its own, so LifespanManager does it explicitly —
    # without this, every request fails with "Task group is not
    # initialized", not an auth error, which is exactly what happened on
    # the first pass at this test.
    async with LifespanManager(asgi_app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi_app),
            base_url=RESOURCE_SERVER_URL,
            headers=headers,
        )
        async with http_client:
            async with streamable_http_client(f"{RESOURCE_SERVER_URL}mcp", http_client=http_client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("server_health", {})


@pytest.mark.asyncio
async def test_valid_bearer_token_resolves_the_real_mapped_identity(
    test_jwks, test_issuer, test_audience, issue_token, engine, seed_mapping
):
    seed_mapping(
        subject="jdoe@corp.com", environment="prod", target_system="ebs",
        mapped_role="AP_MANAGER", org_ids=["204"],
    )
    app = build_authed_app(test_jwks, test_issuer, test_audience, engine)
    token = issue_token(subject="jdoe@corp.com")

    result = await call_over_real_http(app, token)

    assert not result.is_error
    assert '"mapped_role": "AP_MANAGER"' in result.content[0].text
    assert '"204"' in result.content[0].text


@pytest.mark.asyncio
async def test_valid_token_for_unmapped_subject_gets_a_clean_tool_error(
    test_jwks, test_issuer, test_audience, issue_token, engine
):
    app = build_authed_app(test_jwks, test_issuer, test_audience, engine)
    token = issue_token(subject="never-onboarded@corp.com")

    result = await call_over_real_http(app, token)

    assert result.is_error
    assert "No identity mapping" in result.content[0].text


async def raw_status_for(app: MCPServer, token: str | None) -> int:
    """Bypasses ClientSession/streamable_http_client entirely — those wrap
    a non-2xx response in an ExceptionGroup that's awkward to assert on
    precisely. A raw POST gets the actual HTTP status code EntraTokenVerifier
    and the SDK's auth middleware produced, which is the real thing being
    proven here: rejection happens at the transport layer, before any MCP
    session or tool code runs at all.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    asgi_app = app.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1"], allowed_origins=["http://127.0.0.1"]
        )
    )
    async with LifespanManager(asgi_app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi_app), base_url=RESOURCE_SERVER_URL, headers=headers
        ) as client:
            response = await client.post(
                "mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            )
            return response.status_code


@pytest.mark.asyncio
async def test_missing_bearer_token_is_rejected_with_401(test_jwks, test_issuer, test_audience, engine):
    app = build_authed_app(test_jwks, test_issuer, test_audience, engine)
    assert await raw_status_for(app, token=None) == 401


@pytest.mark.asyncio
async def test_forged_token_is_rejected_with_401(test_jwks, test_issuer, test_audience, engine):
    app = build_authed_app(test_jwks, test_issuer, test_audience, engine)
    assert await raw_status_for(app, token="not-a-real-jwt-at-all") == 401
