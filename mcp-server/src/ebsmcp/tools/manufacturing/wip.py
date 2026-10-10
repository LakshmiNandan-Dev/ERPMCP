"""Shop-floor execution (WIP): job status ageing, stuck interface
transactions, component shortages, and per-operation progress.

Scoping: every WIP table carries the inventory ORGANIZATION_ID, scoped
through APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT like the SCM
inventory tools.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Code values
assumed, all to verify on first live access: WIP_DISCRETE_JOBS.STATUS_TYPE
(1 unreleased, 3 released, 4 complete, 5 complete-no-charges, 6 on hold,
7 cancelled, 12 closed, 14 pending close, 15 failed close); interface
PROCESS_STATUS (1 pending, 2 running, 3 error); error text joined from
WIP_TXN_INTERFACE_ERRORS on TRANSACTION_ID. "Unstarted" for a released job
is approximated as QUANTITY_COMPLETED = 0 — it does not look at move
transactions. Completion-transaction errors sit in the material interface;
use inventory_transaction_errors for those.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet
from ebsmcp.tools.scoping import ROW_CAP, inventory_org_filter, run_scoped_list

JOB_STATUS_LABELS: dict[int, str] = {
    1: "Unreleased",
    3: "Released",
    4: "Complete",
    5: "Complete - No Charges",
    6: "On Hold",
    7: "Cancelled",
    12: "Closed",
    14: "Pending Close",
    15: "Failed Close",
}

PROCESS_STATUS_LABELS = {1: "Pending", 2: "Running", 3: "Error"}

PendingKind = Literal["move", "resource"]

_ORG_JOIN = "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = {alias}.organization_id "

# Free on-hand for a component, summed across the organization. Does not net
# reservations — a requirement can look covered here and still be reserved
# elsewhere. Repeated verbatim in the select list and the shortage predicate.
_ONHAND_SUBQUERY = (
    "(SELECT NVL(SUM(moq.transaction_quantity), 0) "
    "FROM APPS.MTL_ONHAND_QUANTITIES_DETAIL moq "
    "WHERE moq.inventory_item_id = wro.inventory_item_id "
    "AND moq.organization_id = wro.organization_id)"
)


def build_job_status_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    aged_days: int,
    include_closed: bool,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One row per org/status with
    counts of released-but-unstarted and complete-but-not-closed jobs older
    than aged_days."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    if not include_closed:
        where.append("wdj.status_type NOT IN (7, 12)")
    binds["aged_days"] = aged_days

    sql = (
        "SELECT ood.organization_code, wdj.status_type, COUNT(*) AS job_count, "
        "MIN(wdj.creation_date) AS oldest_created, "
        "SUM(CASE WHEN wdj.status_type = 3 AND NVL(wdj.quantity_completed, 0) = 0 "
        "AND wdj.date_released < SYSDATE - :aged_days THEN 1 ELSE 0 END) "
        "AS released_unstarted_aged, "
        "SUM(CASE WHEN wdj.status_type = 4 "
        "AND wdj.date_completed < SYSDATE - :aged_days THEN 1 ELSE 0 END) "
        "AS complete_not_closed_aged, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WIP_DISCRETE_JOBS wdj "
        + _ORG_JOIN.format(alias="wdj")
        + f"WHERE {' AND '.join(where)} "
        "GROUP BY ood.organization_code, wdj.status_type "
        "ORDER BY ood.organization_code, wdj.status_type "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_job_status(rows: list[dict]) -> list[dict]:
    """Attach the status label so the model never decodes STATUS_TYPE."""
    return [
        {**r, "status": JOB_STATUS_LABELS.get(r.get("status_type"), f"Unknown ({r.get('status_type')})")}
        for r in rows
    ]


def build_pending_transactions_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    kind: PendingKind,
    include_pending: bool,
    job_name: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Interface rows in error (and,
    with include_pending, those still waiting or running), oldest first,
    with the interface error text."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append(
        "wti.process_status IN (1, 2, 3)" if include_pending else "wti.process_status = 3"
    )
    if job_name:
        where.append("we.wip_entity_name = :job_name")
        binds["job_name"] = job_name

    if kind == "move":
        table = "APPS.WIP_MOVE_TXN_INTERFACE"
        detail = (
            "wti.fm_operation_seq_num, wti.to_operation_seq_num, "
            "wti.transaction_quantity, "
        )
    else:
        table = "APPS.WIP_COST_TXN_INTERFACE"
        detail = "wti.operation_seq_num, wti.transaction_quantity, "

    sql = (
        "SELECT ood.organization_code, we.wip_entity_name AS job_name, "
        "wti.transaction_id, wti.transaction_date, wti.process_status, "
        f"{detail}"
        "wtie.error_column, wtie.error_message, "
        "COUNT(*) OVER () AS total_count "
        f"FROM {table} wti "
        + _ORG_JOIN.format(alias="wti")
        + "LEFT JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wti.wip_entity_id "
        "LEFT JOIN APPS.WIP_TXN_INTERFACE_ERRORS wtie "
        "ON wtie.transaction_id = wti.transaction_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wti.transaction_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_pending_transactions(rows: list[dict]) -> list[dict]:
    return [
        {**r, "process_status_label": PROCESS_STATUS_LABELS.get(r.get("process_status"), "Unknown")}
        for r in rows
    ]


def build_material_shortages_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    job_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Open-job component requirements
    whose outstanding quantity exceeds organization on-hand, earliest need
    first. Phantom (supply type 6) components are excluded."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    outstanding = "wro.required_quantity - NVL(wro.quantity_issued, 0)"
    where += [
        "wdj.status_type IN (1, 3)",
        "NVL(wro.wip_supply_type, 0) <> 6",
        f"{outstanding} > 0",
        f"{outstanding} > {_ONHAND_SUBQUERY}",
    ]
    if job_name:
        where.append("we.wip_entity_name = :job_name")
        binds["job_name"] = job_name

    sql = (
        "SELECT ood.organization_code, we.wip_entity_name AS job_name, "
        "wdj.status_type, wro.operation_seq_num, msi.segment1 AS component, "
        "wro.date_required, wro.required_quantity, "
        "NVL(wro.quantity_issued, 0) AS quantity_issued, "
        f"{outstanding} AS quantity_outstanding, "
        f"{_ONHAND_SUBQUERY} AS onhand_quantity, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WIP_REQUIREMENT_OPERATIONS wro "
        "JOIN APPS.WIP_DISCRETE_JOBS wdj "
        "ON wdj.wip_entity_id = wro.wip_entity_id "
        "AND wdj.organization_id = wro.organization_id "
        "JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wro.wip_entity_id "
        + _ORG_JOIN.format(alias="wro")
        + "JOIN APPS.MTL_SYSTEM_ITEMS_B msi "
        "ON msi.inventory_item_id = wro.inventory_item_id "
        "AND msi.organization_id = wro.organization_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wro.date_required, we.wip_entity_name, wro.operation_seq_num "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_job_progress_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    job_name: str,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Quantity at each operation step
    for one job, in routing order."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("we.wip_entity_name = :job_name")
    binds["job_name"] = job_name

    sql = (
        "SELECT ood.organization_code, we.wip_entity_name AS job_name, "
        "wo.operation_seq_num, bd.department_code, wo.description, "
        "wo.quantity_in_queue, wo.quantity_running, wo.quantity_waiting_to_move, "
        "wo.quantity_rejected, wo.quantity_scrapped, wo.quantity_completed, "
        "wo.scheduled_start_date, wo.scheduled_completion_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WIP_OPERATIONS wo "
        "JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wo.wip_entity_id "
        "AND we.organization_id = wo.organization_id "
        + _ORG_JOIN.format(alias="wo")
        + "LEFT JOIN APPS.BOM_DEPARTMENTS bd ON bd.department_id = wo.department_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY wo.operation_seq_num "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_shortages(rows: list[dict]) -> list[dict]:
    return [
        {**r, "job_status": JOB_STATUS_LABELS.get(r.get("status_type"), "Unknown")} for r in rows
    ]


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def wip_job_status(
        organization_code: str | None = None,
        aged_days: int = 7,
        include_closed: bool = False,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Count discrete jobs by status per inventory organization, with how
        many released jobs have had nothing completed for more than
        aged_days and how many complete jobs have sat unclosed that long.
        Closed and cancelled jobs are excluded unless include_closed.
        Capped at 50 groups with total_count. Use diagnose_job to explain
        one specific stuck job. Call list_ebs_instances first if unsure
        which EBS instance names this deployment has.
        """
        params = {
            "organization_code": organization_code, "aged_days": aged_days,
            "include_closed": include_closed, "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="wip_job_status", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_job_status_query(eff, organization_code, aged_days, include_closed),
            key="job_status", annotate=annotate_job_status,
        )

    @app.tool(annotations=read_only)
    def wip_pending_transactions(
        kind: PendingKind = "move",
        include_pending: bool = False,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List shop-floor transactions stuck in the WIP interface tables with
        the interface error text, oldest first. kind is "move" (operation
        moves) or "resource" (resource charges). By default only rows in
        error; include_pending adds rows still pending or running. Job
        completions are in the material interface — use
        inventory_transaction_errors for those. Capped at 50 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {
            "kind": kind, "include_pending": include_pending,
            "organization_code": organization_code, "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="wip_pending_transactions", params=params, org_ids=org_ids,
            instance=instance,
            build=lambda eff: build_pending_transactions_query(
                eff, organization_code, kind, include_pending
            ),
            key="transactions", annotate=annotate_pending_transactions,
        )

    @app.tool(annotations=read_only)
    def job_material_shortages(
        job_name: str | None = None,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List component requirements on unreleased and released jobs whose
        outstanding quantity exceeds the organization's on-hand, earliest
        need date first. Optionally for one job name. On-hand is not netted
        for reservations. Capped at 50 rows with total_count. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"job_name": job_name, "organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="job_material_shortages", params=params, org_ids=org_ids,
            instance=instance,
            build=lambda eff: build_material_shortages_query(eff, organization_code, job_name),
            key="shortages", annotate=annotate_shortages,
        )

    @app.tool(annotations=read_only)
    def job_progress(
        job_name: str,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Show quantity at each operation step of one job — in queue,
        running, waiting to move, rejected, scrapped and completed — in
        routing order. Pass organization_code if the same job name exists in
        more than one organization. Call list_ebs_instances first if unsure
        which EBS instance names this deployment has.
        """
        params = {"job_name": job_name, "organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="job_progress", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_job_progress_query(eff, organization_code, job_name),
            key="operations",
        )


WIP_TOOLSET = ToolSet(name="mfg_wip", register=_register)
