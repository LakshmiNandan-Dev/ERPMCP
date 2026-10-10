"""Inventory tools: on-hand quantity, item lookup, and stuck/errored
transaction-interface rows.

Scoping: MTL_* tables carry ORGANIZATION_ID (the inventory organization),
not the operating-unit ORG_ID that ResolvedIdentity.allowed_org_ids holds.
Every query therefore joins APPS.ORG_ORGANIZATION_DEFINITIONS and filters on
its OPERATING_UNIT, so an inventory org is visible only if its operating unit
is one the caller is entitled to.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Column and
code-value assumptions — notably MTL_TRANSACTIONS_INTERFACE.PROCESS_FLAG = 3
meaning error, and single-segment item numbers in SEGMENT1 — need checking
on first live access. Item numbers are matched exactly/by prefix with the
case given; EBS item numbers are conventionally upper-case.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scm._scope import ROW_CAP, escape_like, org_in_clause


def build_onhand_query(
    org_ids: tuple[str, ...],
    item_number: str | None,
    subinventory: str | None,
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One row per
    organization/item/subinventory with summed on-hand quantity."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope]
    if item_number:
        where.append("msi.segment1 = :item_number")
        binds["item_number"] = item_number
    if subinventory:
        where.append("moq.subinventory_code = :subinventory")
        binds["subinventory"] = subinventory.upper()
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()

    sql = (
        "SELECT ood.organization_code, msi.segment1 AS item_number, "
        "msi.description, moq.subinventory_code, msi.primary_uom_code, "
        "SUM(moq.transaction_quantity) AS onhand_quantity, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.MTL_ONHAND_QUANTITIES_DETAIL moq "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B msi "
        "ON msi.inventory_item_id = moq.inventory_item_id "
        "AND msi.organization_id = moq.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = moq.organization_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY ood.organization_code, msi.segment1, msi.description, "
        "moq.subinventory_code, msi.primary_uom_code "
        "ORDER BY ood.organization_code, msi.segment1, moq.subinventory_code "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_item_lookup_query(
    org_ids: tuple[str, ...],
    item_prefix: str,
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Prefix match on item number;
    LIKE wildcards in the caller's text are escaped, not honoured."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope, "msi.segment1 LIKE :item_prefix ESCAPE '\\'"]
    binds["item_prefix"] = escape_like(item_prefix) + "%"
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()

    sql = (
        "SELECT ood.organization_code, msi.segment1 AS item_number, "
        "msi.description, msi.primary_uom_code, msi.inventory_item_status_code, "
        "msi.item_type, COUNT(*) OVER () AS total_count "
        "FROM APPS.MTL_SYSTEM_ITEMS_B msi "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = msi.organization_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY msi.segment1, ood.organization_code "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_transaction_errors_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Errored material-transaction
    interface rows grouped by error, so a flood of one failure is one row."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope, "mti.process_flag = 3"]
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()

    sql = (
        "SELECT ood.organization_code, mti.error_code, mti.error_explanation, "
        "COUNT(*) AS error_count, MIN(mti.transaction_date) AS oldest_transaction_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.MTL_TRANSACTIONS_INTERFACE mti "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = mti.organization_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY ood.organization_code, mti.error_code, mti.error_explanation "
        "ORDER BY error_count DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _envelope(ctx, identity, effective_org_ids, filters, rows, total, key) -> dict:
    return {
        "environment": ctx.environment,
        "mapped_role": identity.mapped_role,
        "effective_org_ids": list(effective_org_ids),
        "filters": filters,
        "row_cap": ROW_CAP,
        "total_count": total,
        key: rows,
    }


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def onhand_quantity(
        item_number: str | None = None,
        subinventory: str | None = None,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report on-hand quantity per inventory organization, item and
        subinventory, optionally narrowed to one exact item number,
        subinventory and/or inventory organization code (e.g. M1). Results
        are limited to inventory orgs under the caller's permitted operating
        units; org_ids can only narrow that further. Capped at 50 rows with
        total_count giving the true number of matching groups. Use
        item_lookup to find an item number first. Call list_ebs_instances
        first if unsure which EBS instance names this deployment has.
        """
        params = {
            "item_number": item_number,
            "subinventory": subinventory,
            "organization_code": organization_code,
            "org_ids": org_ids,
        }
        with resolve_scoped_call(
            ctx, tool_name="onhand_quantity", target_system="ebs", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_onhand_query(
                effective_org_ids, item_number, subinventory, organization_code
            )
            rows, total = split_total_count(connector.run(sql, binds))
            return _envelope(ctx, identity, effective_org_ids, params, rows, total, "onhand")

    @app.tool(annotations=read_only)
    def item_lookup(
        item_prefix: str,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Find inventory items whose item number starts with item_prefix
        (wildcards are treated literally), optionally within one inventory
        organization code. Returns description, UOM, status and type per
        organization. Capped at 50 rows with total_count. Use this to
        resolve an item number before calling onhand_quantity. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {
            "item_prefix": item_prefix,
            "organization_code": organization_code,
            "org_ids": org_ids,
        }
        with resolve_scoped_call(
            ctx, tool_name="item_lookup", target_system="ebs", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_item_lookup_query(effective_org_ids, item_prefix, organization_code)
            rows, total = split_total_count(connector.run(sql, binds))
            return _envelope(ctx, identity, effective_org_ids, params, rows, total, "items")

    @app.tool(annotations=read_only)
    def inventory_transaction_errors(
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report material transactions stuck in the inventory transaction
        interface with an error, grouped by error code and message with a
        count and the oldest transaction date. The first check when
        receipts, issues or transfers are not reaching on-hand. Capped at
        50 groups with total_count. Call list_ebs_instances first if unsure
        which EBS instance names this deployment has.
        """
        params = {"organization_code": organization_code, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="inventory_transaction_errors", target_system="ebs", params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_transaction_errors_query(effective_org_ids, organization_code)
            rows, total = split_total_count(connector.run(sql, binds))
            return _envelope(ctx, identity, effective_org_ids, params, rows, total, "errors")


INVENTORY_TOOLSET = ToolSet(name="scm_inventory", register=_register)
