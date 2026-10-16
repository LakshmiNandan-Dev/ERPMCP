"""Purchasing tools: purchase-order status lookup and requisitions waiting
on approval.

Scoping: PO_HEADERS_ALL / PO_REQUISITION_HEADERS_ALL carry the operating-unit
ORG_ID directly, so the entitlement scope is applied as `org_id IN (...)`.

Confidence: written from the documented R12 data model and then executed against a live R12.2 instance on 2026-10-15: every query ran
without error, and row semantics were checked only where noted below. Assumptions
to verify on first live access: APPS.AP_SUPPLIERS as the supplier table
(R12; PO_VENDORS is the 11i name), APPS.HR_EMPLOYEES for the preparer name,
the AUTHORIZATION_STATUS code values below, PO_HEADERS_INTERFACE.PROCESS_CODE =
'REJECTED', and that PO_HEADERS_INTERFACE.ORG_ID is populated (rows with a NULL
ORG_ID are excluded by the scope and would be undercounted).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import SCM_DOMAINS, ROW_CAP, escape_like, org_in_clause, scoped_envelope

PoStatus = Literal[
    "APPROVED", "INCOMPLETE", "IN PROCESS", "PRE-APPROVED", "REJECTED", "REQUIRES REAPPROVAL"
]

# Requisitions a human or the approval workflow still owes an answer on.
# Hardcoded, not bound — the caller selects nothing here.
_PENDING_REQ_STATUSES = "('IN PROCESS', 'PRE-APPROVED')"


def build_po_query(
    org_ids: tuple[str, ...],
    po_number: str | None,
    authorization_status: PoStatus | None,
    supplier_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly."""
    scope, binds = org_in_clause("poh.org_id", org_ids)
    where = [scope]
    if po_number:
        where.append("poh.segment1 = :po_number")
        binds["po_number"] = po_number
    if authorization_status:
        where.append("poh.authorization_status = :authorization_status")
        binds["authorization_status"] = authorization_status
    if supplier_name:
        where.append("UPPER(sup.vendor_name) LIKE :supplier_name ESCAPE '\\'")
        binds["supplier_name"] = "%" + escape_like(supplier_name.upper()) + "%"

    sql = (
        "SELECT poh.segment1 AS po_number, poh.type_lookup_code, "
        "poh.authorization_status, poh.closed_code, poh.org_id, "
        "sup.vendor_name AS supplier_name, poh.creation_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.PO_HEADERS_ALL poh "
        "LEFT JOIN APPS.AP_SUPPLIERS sup ON sup.vendor_id = poh.vendor_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY poh.creation_date DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_pending_requisitions_query(
    org_ids: tuple[str, ...],
    min_days_pending: int | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Oldest first: age is the point."""
    scope, binds = org_in_clause("prh.org_id", org_ids)
    where = [scope, f"prh.authorization_status IN {_PENDING_REQ_STATUSES}"]
    if min_days_pending is not None:
        where.append("TRUNC(SYSDATE) - TRUNC(prh.creation_date) >= :min_days_pending")
        binds["min_days_pending"] = min_days_pending

    sql = (
        "SELECT prh.segment1 AS requisition_number, prh.authorization_status, "
        "prh.type_lookup_code, prh.org_id, he.full_name AS preparer, "
        "prh.creation_date, "
        "TRUNC(SYSDATE) - TRUNC(prh.creation_date) AS days_pending, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.PO_REQUISITION_HEADERS_ALL prh "
        "LEFT JOIN APPS.HR_EMPLOYEES he ON he.employee_id = prh.preparer_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY prh.creation_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_overdue_shipments_query(
    org_ids: tuple[str, ...],
    min_days_overdue: int | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Open PO shipments past their
    need-by date with quantity still outstanding, most overdue first."""
    scope, binds = org_in_clause("pll.org_id", org_ids)
    where = [
        scope,
        "pll.shipment_type = 'STANDARD'",
        "NVL(pll.cancel_flag, 'N') = 'N'",
        "pll.need_by_date < TRUNC(SYSDATE)",
        "pll.quantity - NVL(pll.quantity_received, 0) - NVL(pll.quantity_cancelled, 0) > 0",
    ]
    if min_days_overdue is not None:
        where.append("TRUNC(SYSDATE) - TRUNC(pll.need_by_date) >= :min_days_overdue")
        binds["min_days_overdue"] = min_days_overdue

    sql = (
        "SELECT poh.segment1 AS po_number, pol.line_num, pll.shipment_num, "
        "pll.need_by_date, TRUNC(SYSDATE) - TRUNC(pll.need_by_date) AS days_overdue, "
        "pll.quantity - NVL(pll.quantity_received, 0) - NVL(pll.quantity_cancelled, 0) "
        "AS quantity_outstanding, pll.org_id, sup.vendor_name AS supplier_name, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.PO_LINE_LOCATIONS_ALL pll "
        "JOIN APPS.PO_HEADERS_ALL poh ON poh.po_header_id = pll.po_header_id "
        "JOIN APPS.PO_LINES_ALL pol ON pol.po_line_id = pll.po_line_id "
        "LEFT JOIN APPS.AP_SUPPLIERS sup ON sup.vendor_id = poh.vendor_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY pll.need_by_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_po_import_rejections_query(
    org_ids: tuple[str, ...],
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. PO Import header rows the
    import rejected, newest first."""
    scope, binds = org_in_clause("phi.org_id", org_ids)
    sql = (
        "SELECT phi.interface_header_id, phi.document_num, phi.document_type_code, "
        "phi.process_code, phi.org_id, phi.creation_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.PO_HEADERS_INTERFACE phi "
        f"WHERE {scope} AND phi.process_code = 'REJECTED' "
        "ORDER BY phi.creation_date DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def purchase_order_status(
        po_number: str | None = None,
        authorization_status: PoStatus | None = None,
        supplier_name: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Look up purchase orders by exact PO number, authorization status
        (APPROVED, INCOMPLETE, IN PROCESS, PRE-APPROVED, REJECTED,
        REQUIRES REAPPROVAL) and/or part of a supplier name, newest first.
        Limited to the caller's permitted operating units; org_ids can only
        narrow that. Capped at 50 rows with total_count. For requisitions
        awaiting approval use requisitions_pending_approval. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {
            "po_number": po_number,
            "authorization_status": authorization_status,
            "supplier_name": supplier_name,
            "org_ids": org_ids,
        }
        with resolve_scoped_call(
            ctx, tool_name="purchase_order_status", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_po_query(
                effective_org_ids, po_number, authorization_status, supplier_name
            )
            rows, total = split_total_count(connector.run(sql, binds))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "filters": params,
                "row_cap": ROW_CAP,
                "total_count": total,
                "purchase_orders": rows,
            }

    @app.tool(annotations=read_only)
    def requisitions_pending_approval(
        min_days_pending: int | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List purchase requisitions still awaiting approval (IN PROCESS
        or PRE-APPROVED), oldest first, with preparer and days pending.
        min_days_pending narrows to those waiting at least that long.
        Capped at 50 rows with total_count. Call list_ebs_instances first
        if unsure which EBS instance names this deployment has.
        """
        params = {"min_days_pending": min_days_pending, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="requisitions_pending_approval", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_pending_requisitions_query(effective_org_ids, min_days_pending)
            rows, total = split_total_count(connector.run(sql, binds))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "filters": params,
                "row_cap": ROW_CAP,
                "total_count": total,
                "requisitions": rows,
            }

    @app.tool(annotations=read_only)
    def overdue_po_shipments(
        min_days_overdue: int | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List open purchase-order shipments past their need-by date that
        still have quantity outstanding, most overdue first, with supplier
        and days overdue. min_days_overdue narrows to those at least that
        late. Capped at 50 rows with total_count. Call list_ebs_instances
        first if unsure which EBS instance names this deployment has.
        """
        params = {"min_days_overdue": min_days_overdue, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="overdue_po_shipments", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_overdue_shipments_query(effective_org_ids, min_days_overdue)
            rows, total = split_total_count(connector.run(sql, binds))
            return scoped_envelope(
                ctx, identity, effective_org_ids, params, rows, total, "shipments"
            )

    @app.tool(annotations=read_only)
    def po_import_rejections(
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List PO Import header rows rejected by the import
        (PO_HEADERS_INTERFACE, process code REJECTED), newest first. Use
        when a batch of purchase orders did not make it into Purchasing.
        Capped at 50 rows with total_count. Call list_ebs_instances first
        if unsure which EBS instance names this deployment has.
        """
        params = {"org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="po_import_rejections", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_po_import_rejections_query(effective_org_ids)
            rows, total = split_total_count(connector.run(sql, binds))
            return scoped_envelope(
                ctx, identity, effective_org_ids, params, rows, total, "rejections"
            )


PURCHASING_TOOLSET = ToolSet(name="scm_purchasing", register=_register)
