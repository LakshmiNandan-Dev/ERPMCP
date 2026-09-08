"""Oracle Workflow health: one parameterized tool over
WF_ITEM_ACTIVITY_STATUSES, same consolidation reasoning as
concurrent_processing.py — a status enum instead of a tool per status,
free-text filters always bound rather than string-interpolated.

Schema ownership caveat carried over from the original single-purpose
version: WF_ITEM_ACTIVITY_STATUSES and its columns are standard Oracle
Workflow structures, but Workflow's schema owner has varied historically
(older releases used OWF_MGR; R12 typically bundles it under APPLSYS) —
confirm APPLSYS is correct against the target release before relying on
this in production.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

ActivityStatus = Literal["error", "active", "suspended", "deferred"]

# Hardcoded per-status literal (not bound) — an LLM can only select which
# of these four pre-vetted conditions applies, never author WHERE text;
# item_type/since are the caller-supplied values, and those are always
# bound. Same split as concurrent_processing.build_query.
_STATUS_CONDITIONS: dict[ActivityStatus, str] = {
    "error": "wias.activity_status = 'ERROR'",
    "active": "wias.activity_status = 'ACTIVE'",
    "suspended": "wias.activity_status = 'SUSPEND'",
    "deferred": "wias.activity_status = 'DEFERRED'",
}


def build_query(
    status: ActivityStatus,
    item_type: str | None,
    since: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly — see
    test_workflow_activities_query.py."""
    where = [_STATUS_CONDITIONS[status]]
    binds: dict[str, Any] = {}

    if item_type:
        where.append("wias.item_type = :item_type")
        # Workflow item type codes are conventionally upper-case (e.g. POAPPRV).
        binds["item_type"] = item_type.upper()

    if since:
        where.append("wias.begin_date >= TO_DATE(:since, 'YYYY-MM-DD')")
        binds["since"] = since

    sql = (
        "SELECT wias.item_type, wias.item_key, wias.activity_status, "
        "wias.assigned_user, wias.begin_date "
        "FROM APPLSYS.WF_ITEM_ACTIVITY_STATUSES wias "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wias.begin_date DESC"
    )
    return sql, binds


def build_notifications_query(
    notification_id: int | None,
    mail_status: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly — see
    test_notifications_query.py. notification_id given -> single-row
    detail lookup; omitted -> fleet aggregate grouped by mail_status,
    optionally narrowed by a bound mail_status filter.
    """
    if notification_id is not None:
        return (
            "SELECT wn.notification_id, wn.message_type, wn.message_name, wn.subject, "
            "wn.status, wn.mail_status, wn.recipient_role, wn.begin_date, wn.end_date, wn.responder "
            "FROM APPLSYS.WF_NOTIFICATIONS wn "
            "WHERE wn.notification_id = :notification_id",
            {"notification_id": notification_id},
        )

    binds: dict[str, Any] = {}
    where = ""
    if mail_status:
        where = "WHERE wn.mail_status = :mail_status "
        binds["mail_status"] = mail_status.upper()

    sql = (
        "SELECT wn.mail_status, COUNT(*) AS notification_count "
        "FROM APPLSYS.WF_NOTIFICATIONS wn "
        f"{where}"
        "GROUP BY wn.mail_status "
        "ORDER BY notification_count DESC"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def workflow_activities(
        status: ActivityStatus = "error",
        item_type: str | None = None,
        since: str | None = None,
        instance: str | None = None,
    ) -> dict:
        """List workflow item activities matching status (error, active,
        suspended, or deferred), optionally narrowed to one workflow item
        type (e.g. POAPPRV) and/or a start date (YYYY-MM-DD). Defaults to
        error, the standard first check for a stuck workflow process.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx,
            tool_name="workflow_activities",
            target_system="ebs_dba",
            params={"status": status, "item_type": item_type, "since": since},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_query(status, item_type, since)
            rows = connector.run(sql, binds)

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "filters": {"status": status, "item_type": item_type, "since": since},
                "activities": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def workflow_backlog(instance: str | None = None) -> dict:
        """Report background-engine queue depth: counts of workflow item
        activities currently deferred, active, or waiting. The standard
        first check for whether the workflow engine is falling behind. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="workflow_backlog", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT wias.activity_status, COUNT(*) AS activity_count "
                "FROM APPLSYS.WF_ITEM_ACTIVITY_STATUSES wias "
                "WHERE wias.activity_status IN ('DEFERRED', 'ACTIVE', 'WAITING') "
                "GROUP BY wias.activity_status "
                "ORDER BY activity_count DESC"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "backlog_by_status": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def mailer_component_status(instance: str | None = None) -> dict:
        """Report running/stopped status and last update time for every
        service component with "Mailer" in its name — the Workflow
        Notification Mailer, inbound and outbound instances included. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="mailer_component_status", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT fsc.component_id, fsc.component_name, fsc.component_status, fsc.last_update_date "
                "FROM APPLSYS.FND_SVC_COMPONENTS fsc "
                "WHERE UPPER(fsc.component_name) LIKE '%MAILER%' "
                "ORDER BY fsc.component_name"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "components": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def notifications(notification_id: int | None = None, mail_status: str | None = None, instance: str | None = None) -> dict:
        """Look up one notification by ID (full detail: status, mail
        status, recipient, response), or omit notification_id for a
        fleet-level count grouped by mail_status, optionally narrowed to
        one mail_status value — the standard "how many failed vs. mailed
        vs. still pending" check before drilling into one notification.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx,
            tool_name="notifications",
            target_system="ebs_dba",
            params={"notification_id": notification_id, "mail_status": mail_status},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_notifications_query(notification_id, mail_status)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "notification_id": notification_id,
                "mail_status_filter": mail_status,
                "results": rows,
            }


WORKFLOW_TOOLSET = ToolSet(name="workflow", register=_register)
