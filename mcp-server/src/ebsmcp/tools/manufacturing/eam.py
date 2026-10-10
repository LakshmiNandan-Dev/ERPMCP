"""Maintenance (EAM): open and overdue work orders by asset or department.

Not covered yet: pm_schedule_due and asset_failure_history. Both read EAM
tables (the PM scheduling table, failure analysis) whose column sets differ
between R12.1 and R12.2 and could not be written reliably without a live
instance to introspect. Add them once one is available.

Scoping: work orders carry the inventory ORGANIZATION_ID, scoped through
APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Assumptions
to verify: EAM work orders are WIP_ENTITIES.ENTITY_TYPE = 6 with their asset
in WIP_DISCRETE_JOBS.MAINTENANCE_OBJECT_ID (MAINTENANCE_OBJECT_TYPE 3 =
CSI_ITEM_INSTANCES.INSTANCE_ID); open means STATUS_TYPE 1, 3 or 6
(unreleased, released, on hold) — EAM adds its own work-order statuses on
some instances, which this would miss.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet
from ebsmcp.tools.scoping import ROW_CAP, inventory_org_filter, run_scoped_list


def build_asset_work_orders_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    asset_number: str | None,
    department_code: str | None,
    overdue_only: bool,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Open EAM work orders, most
    overdue first."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("wdj.status_type IN (1, 3, 6)")
    if asset_number:
        where.append("cii.instance_number = :asset_number")
        binds["asset_number"] = asset_number
    if department_code:
        where.append("bd.department_code = :department_code")
        binds["department_code"] = department_code.upper()
    if overdue_only:
        where.append("wdj.scheduled_completion_date < SYSDATE")

    sql = (
        "SELECT ood.organization_code, we.wip_entity_name AS work_order, we.description, "
        "wdj.status_type, cii.instance_number AS asset_number, "
        "bd.department_code AS owning_department, wdj.scheduled_start_date, "
        "wdj.scheduled_completion_date, "
        "GREATEST(TRUNC(SYSDATE) - TRUNC(wdj.scheduled_completion_date), 0) AS days_overdue, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WIP_DISCRETE_JOBS wdj "
        "JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wdj.wip_entity_id "
        "AND we.organization_id = wdj.organization_id AND we.entity_type = 6 "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = wdj.organization_id "
        "LEFT JOIN APPS.CSI_ITEM_INSTANCES cii ON cii.instance_id = wdj.maintenance_object_id "
        "AND wdj.maintenance_object_type = 3 "
        "LEFT JOIN APPS.BOM_DEPARTMENTS bd ON bd.department_id = wdj.owning_department "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wdj.scheduled_completion_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def asset_work_orders(
        asset_number: str | None = None,
        department_code: str | None = None,
        overdue_only: bool = False,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List open (unreleased, released or on-hold) maintenance work
        orders, most overdue first, optionally for one asset number, owning
        department code, or only those past their scheduled completion.
        Capped at 50 rows with total_count. Call list_ebs_instances first
        if unsure which EBS instance names this deployment has.
        """
        params = {
            "asset_number": asset_number, "department_code": department_code,
            "overdue_only": overdue_only, "organization_code": organization_code,
            "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="asset_work_orders", params=params, org_ids=org_ids,
            instance=instance,
            build=lambda eff: build_asset_work_orders_query(
                eff, organization_code, asset_number, department_code, overdue_only
            ),
            key="work_orders",
        )


EAM_TOOLSET = ToolSet(name="mfg_eam", register=_register)
