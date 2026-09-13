"""notification_diagnosis: composed tool correlating notification detail
+ mailer component status + the raising workflow activity — why a
specific notification hasn't gone out or been actioned, in one call, per
the architecture doc's description.

Confidence: MEDIUM overall. Reuses workflow.py's build_notifications_query
for the detail lookup (MEDIUM-HIGH — see that module) and
mailer_component_status's query (MEDIUM — see workflow.py's own caveat on
FND_SVC_COMPONENTS column names). The one new piece here —
WF_ITEM_ACTIVITY_STATUSES.NOTIFICATION_ID correlating an activity back to
the notification it raised — is this tool's own weakest link: I have
moderate confidence the column exists (it's the natural way an activity
would track which notification it raised), not cross-checked against a
real instance.

"Component not RUNNING" is treated as suspect for any status value other
than 'RUNNING' — a conservative default given real uncertainty about the
full FND_SVC_COMPONENTS.COMPONENT_STATUS vocabulary (see workflow.py).
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ebsmcp.tools.dba.workflow import build_notifications_query
from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

_ACTIVITY_LOOKUP_SQL = (
    "SELECT wias.item_type, wias.item_key, wias.activity_status, wias.assigned_user "
    "FROM APPS.WF_ITEM_ACTIVITY_STATUSES wias "
    "WHERE wias.notification_id = :notification_id"
)

_MAILER_STATUS_SQL = (
    "SELECT fsc.component_id, fsc.component_name, fsc.component_status, fsc.last_update_date "
    "FROM APPS.FND_SVC_COMPONENTS fsc "
    "WHERE UPPER(fsc.component_name) LIKE '%MAILER%' "
    "ORDER BY fsc.component_name"
)

_STUCK_MAIL_STATUSES = frozenset({"MAILED", "FAILED"})


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def notification_diagnosis(notification_id: int, instance: str | None = None) -> dict:
        """Diagnose why a specific notification hasn't gone out or been
        actioned: looks up the notification, the workflow activity that
        raised it (flagging ERROR/SUSPEND as the likely cause over mail
        delivery), and — if the mail status looks stuck — the mailer
        component's own running status.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx,
            tool_name="notification_diagnosis",
            target_system="ebs_dba",
            params={"notification_id": notification_id},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_notifications_query(notification_id, None)
            notification_rows = connector.run(sql, binds)
            if not notification_rows:
                raise ToolError(f"No notification found with notification_id={notification_id}.")
            notification = notification_rows[0]

            causes: list[str] = []

            activity_rows = connector.run(_ACTIVITY_LOOKUP_SQL, {"notification_id": notification_id})
            activity = activity_rows[0] if activity_rows else None
            if activity and activity["activity_status"] in ("ERROR", "SUSPEND"):
                causes.append(
                    f"Raising workflow activity ({activity['item_type']}/{activity['item_key']}) "
                    f"is in {activity['activity_status']} status."
                )

            mailer_components: list[dict[str, Any]] = []
            if notification.get("mail_status") in _STUCK_MAIL_STATUSES:
                mailer_components = connector.run(_MAILER_STATUS_SQL)
                not_running = [c for c in mailer_components if c.get("component_status") != "RUNNING"]
                if not_running:
                    names = ", ".join(c["component_name"] for c in not_running)
                    causes.append(f"Mailer component(s) not running: {names}.")

            if not causes:
                causes.append("No obvious cause found — notification and raising activity both look healthy.")

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "notification": notification,
                "raising_activity": activity,
                "mailer_components": mailer_components,
                "likely_causes": causes,
            }


NOTIFICATION_DIAGNOSIS_TOOLSET = ToolSet(name="notification_diagnosis", register=_register)
