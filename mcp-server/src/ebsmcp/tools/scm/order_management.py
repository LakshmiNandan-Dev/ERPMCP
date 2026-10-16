"""Order Management tools: sales-order lookup and orders on hold.

Scoping: OE_ORDER_HEADERS_ALL carries the operating-unit ORG_ID, applied as
`org_id IN (...)`.

Confidence: written from the documented R12 data model and then executed against a live R12.2 instance on 2026-10-15: every query ran
without error, and row semantics were checked only where noted below. Assumptions
to verify on first live access: the OE_ORDER_HOLDS_ALL -> OE_HOLD_SOURCES_ALL
-> OE_HOLD_DEFINITIONS join chain, RELEASED_FLAG = 'N' meaning an active hold,
and that LINE_ID IS NULL isolates order-level (header) holds. Line-level
holds are deliberately not reported by orders_on_hold. For order_import_errors:
OE_HEADERS_IFACE_ALL.ERROR_FLAG = 'Y' marks a failed row; detail messages live in
OE_PROCESSING_MSGS and are not joined here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import SCM_DOMAINS, ROW_CAP, escape_like, org_in_clause, scoped_envelope

_CUSTOMER_JOINS = (
    "LEFT JOIN APPS.HZ_CUST_ACCOUNTS hca ON hca.cust_account_id = ooh.sold_to_org_id "
    "LEFT JOIN APPS.HZ_PARTIES hp ON hp.party_id = hca.party_id "
)


def build_order_query(
    org_ids: tuple[str, ...],
    order_number: int | None,
    flow_status_code: str | None,
    customer_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly."""
    scope, binds = org_in_clause("ooh.org_id", org_ids)
    where = [scope]
    if order_number is not None:
        where.append("ooh.order_number = :order_number")
        binds["order_number"] = order_number
    if flow_status_code:
        where.append("ooh.flow_status_code = :flow_status_code")
        binds["flow_status_code"] = flow_status_code.upper()
    if customer_name:
        where.append("UPPER(hp.party_name) LIKE :customer_name ESCAPE '\\'")
        binds["customer_name"] = "%" + escape_like(customer_name.upper()) + "%"

    sql = (
        "SELECT ooh.order_number, ooh.flow_status_code, ooh.booked_flag, "
        "ooh.ordered_date, ooh.org_id, hp.party_name AS customer_name, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.OE_ORDER_HEADERS_ALL ooh "
        f"{_CUSTOMER_JOINS}"
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ooh.ordered_date DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_orders_on_hold_query(
    org_ids: tuple[str, ...],
    hold_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Active order-level holds,
    longest-held first."""
    scope, binds = org_in_clause("ooh.org_id", org_ids)
    where = [scope, "oh.released_flag = 'N'", "oh.line_id IS NULL"]
    if hold_name:
        where.append("ohd.name = :hold_name")
        binds["hold_name"] = hold_name

    sql = (
        "SELECT ooh.order_number, ohd.name AS hold_name, oh.creation_date AS hold_date, "
        "TRUNC(SYSDATE) - TRUNC(oh.creation_date) AS days_on_hold, "
        "ooh.org_id, hp.party_name AS customer_name, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.OE_ORDER_HOLDS_ALL oh "
        "JOIN APPS.OE_ORDER_HEADERS_ALL ooh ON ooh.header_id = oh.header_id "
        "JOIN APPS.OE_HOLD_SOURCES_ALL ohs ON ohs.hold_source_id = oh.hold_source_id "
        "JOIN APPS.OE_HOLD_DEFINITIONS ohd ON ohd.hold_id = ohs.hold_id "
        f"{_CUSTOMER_JOINS}"
        f"WHERE {' AND '.join(where)} "
        "ORDER BY oh.creation_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_order_import_errors_query(
    org_ids: tuple[str, ...],
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Order Import header rows
    flagged in error, newest first."""
    scope, binds = org_in_clause("ohi.org_id", org_ids)
    sql = (
        "SELECT ohi.orig_sys_document_ref, ohi.order_source_id, ohi.order_number, "
        "ohi.org_id, ohi.creation_date, COUNT(*) OVER () AS total_count "
        "FROM APPS.OE_HEADERS_IFACE_ALL ohi "
        f"WHERE {scope} AND ohi.error_flag = 'Y' "
        "ORDER BY ohi.creation_date DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def sales_order_status(
        order_number: int | None = None,
        flow_status_code: str | None = None,
        customer_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Look up sales orders by order number, workflow status code (e.g.
        BOOKED, ENTERED, CLOSED, CANCELLED) and/or part of a customer name,
        newest first. Limited to the caller's permitted operating units;
        org_ids can only narrow that. Capped at 50 rows with total_count.
        To see only orders blocked by a hold use orders_on_hold. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {
            "order_number": order_number,
            "flow_status_code": flow_status_code,
            "customer_name": customer_name,
            "org_ids": org_ids,
        }
        with resolve_scoped_call(
            ctx, tool_name="sales_order_status", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_order_query(
                effective_org_ids, order_number, flow_status_code, customer_name
            )
            rows, total = split_total_count(connector.run(sql, binds))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "filters": params,
                "row_cap": ROW_CAP,
                "total_count": total,
                "orders": rows,
            }

    @app.tool(annotations=read_only)
    def orders_on_hold(
        hold_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List sales orders with an active order-level hold, longest-held
        first, with the hold name, days on hold and customer. hold_name
        narrows to one exact hold definition name. Line-level holds are not
        included. Capped at 50 rows with total_count. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"hold_name": hold_name, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="orders_on_hold", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_orders_on_hold_query(effective_org_ids, hold_name)
            rows, total = split_total_count(connector.run(sql, binds))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "filters": params,
                "row_cap": ROW_CAP,
                "total_count": total,
                "orders_on_hold": rows,
            }

    @app.tool(annotations=read_only)
    def order_import_errors(
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List Order Import header rows flagged in error
        (OE_HEADERS_IFACE_ALL), newest first, with the source system
        document reference. Use when orders from an external source or EDI
        did not create. Capped at 50 rows with total_count. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="order_import_errors", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_order_import_errors_query(effective_org_ids)
            rows, total = split_total_count(connector.run(sql, binds))
            return scoped_envelope(
                ctx, identity, effective_org_ids, params, rows, total, "import_errors"
            )


ORDER_MANAGEMENT_TOOLSET = ToolSet(name="scm_order_management", register=_register)
