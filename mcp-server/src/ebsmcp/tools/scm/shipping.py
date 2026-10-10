"""Shipping Execution tools: open deliveries and backordered delivery lines.

Scoping: WSH_NEW_DELIVERIES / WSH_DELIVERY_DETAILS carry the shipping
(inventory) ORGANIZATION_ID, scoped through
APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT like the Inventory tools.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance. Assumptions to verify: WSH_NEW_DELIVERIES
.STATUS_CODE 'OP' = open, and WSH_DELIVERY_DETAILS.RELEASED_STATUS 'B' =
backordered with SOURCE_CODE 'OE' for sales-order lines.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import ROW_CAP, org_in_clause, scoped_envelope


def build_open_deliveries_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Open deliveries, oldest first."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope, "wnd.status_code = 'OP'"]
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()

    sql = (
        "SELECT wnd.name AS delivery_name, ood.organization_code, wnd.status_code, "
        "wnd.initial_pickup_date, wnd.creation_date, "
        "TRUNC(SYSDATE) - TRUNC(wnd.creation_date) AS days_open, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WSH_NEW_DELIVERIES wnd "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = wnd.organization_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wnd.creation_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_backordered_lines_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Sales-order lines backordered
    at pick release, oldest first."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope, "wdd.released_status = 'B'", "wdd.source_code = 'OE'"]
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()

    sql = (
        "SELECT wdd.source_header_number AS order_number, "
        "wdd.source_line_number AS line_number, ood.organization_code, "
        "msi.segment1 AS item_number, wdd.requested_quantity, "
        "wdd.requested_quantity_uom, wdd.creation_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WSH_DELIVERY_DETAILS wdd "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = wdd.organization_id "
        "LEFT JOIN APPS.MTL_SYSTEM_ITEMS_B msi "
        "ON msi.inventory_item_id = wdd.inventory_item_id "
        "AND msi.organization_id = wdd.organization_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wdd.creation_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def open_deliveries(
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List shipping deliveries still open (not yet ship-confirmed),
        oldest first with days open, optionally for one inventory
        organization code. Capped at 50 rows with total_count. For lines
        that failed to allocate use backordered_lines. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"organization_code": organization_code, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="open_deliveries", target_system="ebs", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_open_deliveries_query(effective_org_ids, organization_code)
            rows, total = split_total_count(connector.run(sql, binds))
            return scoped_envelope(
                ctx, identity, effective_org_ids, params, rows, total, "deliveries"
            )

    @app.tool(annotations=read_only)
    def backordered_lines(
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List sales-order lines backordered at pick release (could not be
        fully allocated from on-hand), oldest first with order number, item
        and requested quantity. Cross-check against onhand_quantity.
        Capped at 50 rows with total_count. Call list_ebs_instances first
        if unsure which EBS instance names this deployment has.
        """
        params = {"organization_code": organization_code, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="backordered_lines", target_system="ebs", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_backordered_lines_query(effective_org_ids, organization_code)
            rows, total = split_total_count(connector.run(sql, binds))
            return scoped_envelope(ctx, identity, effective_org_ids, params, rows, total, "lines")


SHIPPING_TOOLSET = ToolSet(name="scm_shipping", register=_register)
