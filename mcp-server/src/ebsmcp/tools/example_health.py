"""One placeholder tool, proving the full request pipeline end to end
before any real DBA tool exists: resolve identity -> apply entitlement
filter -> run a GV$/schema-qualified query through the connector -> audit
log the call, success or failure. Every real tool from the DBA catalog
(diagnose_request, tablespace_usage, ...) follows this exact same shape,
via resolve_scoped_call — this is deliberately the simplest possible
instance of it.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def server_health(instance: str | None = None) -> dict:
        """Report whether the MCP server can reach its configured EBS
        connector and resolve the calling identity. Takes no required
        parameters — useful as a first call when wiring up a new client or
        environment. Pass instance to check a specific configured EBS
        instance when more than one is available.
        """
        with resolve_scoped_call(
            ctx,
            tool_name="server_health",
            target_system="ebs",
            params={},
            requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            rows = connector.run(
                "SELECT instance_name, status, database_status FROM GV$INSTANCE"
            )

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "instance": rows[0] if rows else None,
            }


HEALTH_TOOLSET = ToolSet(name="health", register=_register)
