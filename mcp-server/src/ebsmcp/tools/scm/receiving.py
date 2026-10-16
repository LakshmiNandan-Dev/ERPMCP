"""Receiving tools: errored rows in the receiving transaction interface.

Scoping: RCV_TRANSACTIONS_INTERFACE is scoped through the destination
inventory organization (TO_ORGANIZATION_ID) -> APPS.ORG_ORGANIZATION_DEFINITIONS
.OPERATING_UNIT. Rows whose TO_ORGANIZATION_ID is still NULL (not yet
defaulted by the receiving processor) are excluded by that join and would be
undercounted.

Confidence: written from the documented R12 data model and then executed against a live R12.2 instance on 2026-10-15: every query ran
without error, and row semantics were checked only where noted below. Assumptions to verify: PROCESSING_STATUS_CODE =
'ERROR' marks a failed row, and PO_INTERFACE_ERRORS.INTERFACE_TRANSACTION_ID
links error text to the interface row.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import SCM_DOMAINS, ROW_CAP, org_in_clause, scoped_envelope


def build_receiving_errors_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Errored receiving interface
    rows grouped by error message, so one repeated failure is one row."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope, "rti.processing_status_code = 'ERROR'"]
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()

    sql = (
        "SELECT ood.organization_code, pie.error_message, "
        "COUNT(*) AS error_count, MIN(rti.creation_date) AS oldest_creation_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.RCV_TRANSACTIONS_INTERFACE rti "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = rti.to_organization_id "
        "LEFT JOIN APPS.PO_INTERFACE_ERRORS pie "
        "ON pie.interface_transaction_id = rti.interface_transaction_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY ood.organization_code, pie.error_message "
        "ORDER BY error_count DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def receiving_interface_errors(
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report receipts stuck in the receiving transaction interface in
        error, grouped by error message with a count and the oldest row
        date, optionally for one inventory organization code. The first
        check when goods are physically received but PO receipts are
        missing. Capped at 50 groups with total_count. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"organization_code": organization_code, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="receiving_interface_errors", target_system="ebs", domains=SCM_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_receiving_errors_query(effective_org_ids, organization_code)
            rows, total = split_total_count(connector.run(sql, binds))
            return scoped_envelope(ctx, identity, effective_org_ids, params, rows, total, "errors")


RECEIVING_TOOLSET = ToolSet(name="scm_receiving", register=_register)
