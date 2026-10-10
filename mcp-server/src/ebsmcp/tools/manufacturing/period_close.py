"""Period close readiness: everything that blocks closing an inventory
accounting period, in one call per organization.

Scoping: per inventory organization, scoped through
APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT.

"Open period" here means the earliest ORG_ACCT_PERIODS row with OPEN_FLAG =
'Y' — the period that would be closed next. Uncosted material transactions
are counted from that period's start date onward; the interface and
pending-transaction counts are totals, since any row there blocks a close
regardless of date.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Assumptions
to verify: ORG_ACCT_PERIODS.OPEN_FLAG 'Y' = open (a period in 'P' pending
close is not treated as open); MTL_MATERIAL_TRANSACTIONS_TEMP holds
unprocessed material transactions; RCV_TRANSACTIONS_INTERFACE rows not yet
COMPLETED block close. The counts are blockers to investigate, not a
guarantee: Oracle's own close checks (e.g. unprocessed shipping
transactions, unreconciled receiving accruals) are not all reproduced here,
so ready_to_close=true means "none of these five checks found anything".
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet
from ebsmcp.tools.scoping import ROW_CAP, inventory_org_filter, run_scoped_list

_EARLIEST_OPEN_START = (
    "(SELECT MIN(oap.period_start_date) FROM APPS.ORG_ACCT_PERIODS oap "
    "WHERE oap.organization_id = ood.organization_id AND oap.open_flag = 'Y')"
)

# check name -> human label, in the order they are reported.
BLOCKER_LABELS = {
    "pending_material_transactions": "Unprocessed material transactions",
    "material_interface_rows": "Rows in the material transaction interface",
    "uncosted_transactions": "Material transactions not yet costed",
    "pending_wip_move_rows": "Pending WIP move transactions",
    "pending_wip_resource_rows": "Pending WIP resource transactions",
    "pending_receiving_rows": "Pending receiving transactions",
}


def build_period_close_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One row per inventory
    organization, each blocker a correlated scalar count."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    sql = (
        "SELECT ood.organization_code, "
        f"{_EARLIEST_OPEN_START} AS earliest_open_period_start, "
        "(SELECT COUNT(*) FROM APPS.MTL_MATERIAL_TRANSACTIONS_TEMP mmtt "
        "WHERE mmtt.organization_id = ood.organization_id) "
        "AS pending_material_transactions, "
        "(SELECT COUNT(*) FROM APPS.MTL_TRANSACTIONS_INTERFACE mti "
        "WHERE mti.organization_id = ood.organization_id) AS material_interface_rows, "
        "(SELECT COUNT(*) FROM APPS.MTL_MATERIAL_TRANSACTIONS mmt "
        "WHERE mmt.organization_id = ood.organization_id "
        "AND mmt.costed_flag IN ('N', 'E') "
        f"AND mmt.transaction_date >= {_EARLIEST_OPEN_START}) AS uncosted_transactions, "
        "(SELECT COUNT(*) FROM APPS.WIP_MOVE_TXN_INTERFACE wmti "
        "WHERE wmti.organization_id = ood.organization_id) AS pending_wip_move_rows, "
        "(SELECT COUNT(*) FROM APPS.WIP_COST_TXN_INTERFACE wcti "
        "WHERE wcti.organization_id = ood.organization_id) AS pending_wip_resource_rows, "
        "(SELECT COUNT(*) FROM APPS.RCV_TRANSACTIONS_INTERFACE rti "
        "WHERE rti.to_organization_id = ood.organization_id "
        "AND rti.processing_status_code <> 'COMPLETED') AS pending_receiving_rows, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ood.organization_code "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_period_close(rows: list[dict]) -> list[dict]:
    """Decide per organization which checks block and whether it is ready,
    so the model never compares counts to zero itself."""
    out = []
    for r in rows:
        blockers = [
            {"check": name, "label": label, "count": int(r.get(name) or 0)}
            for name, label in BLOCKER_LABELS.items()
            if int(r.get(name) or 0) > 0
        ]
        no_open_period = r.get("earliest_open_period_start") is None
        out.append(
            {
                **r,
                "blockers": blockers,
                "no_open_period": no_open_period,
                "ready_to_close": not blockers and not no_open_period,
            }
        )
    return out


def summarize_period_close(rows: list[dict], total: int | None) -> dict:
    return {
        "summary": {
            "organizations_returned": len(rows),
            "organizations_blocked": sum(1 for r in rows if r["blockers"]),
            "organizations_ready": sum(1 for r in rows if r["ready_to_close"]),
            "organizations_without_open_period": sum(1 for r in rows if r["no_open_period"]),
        }
    }


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def inventory_period_close_check(
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report, per inventory organization, everything blocking close of
        its earliest open accounting period: unprocessed material
        transactions, material interface rows, uncosted transactions
        (since the period start), pending WIP move and resource
        transactions, and pending receiving rows. Each organization carries
        a blockers list and a ready_to_close verdict. Drill into a blocker
        with inventory_transaction_errors, uncosted_transactions,
        wip_pending_transactions or receiving_interface_errors. Capped at
        50 organizations with total_count. Call list_ebs_instances first if
        unsure which EBS instance names this deployment has.
        """
        params = {"organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="inventory_period_close_check", params=params, org_ids=org_ids,
            instance=instance, build=lambda eff: build_period_close_query(eff, organization_code),
            key="organizations", annotate=annotate_period_close, summarize=summarize_period_close,
        )


PERIOD_CLOSE_TOOLSET = ToolSet(name="mfg_period_close", register=_register)
