"""diagnose_job: one composite call that explains why a specific discrete job
is stuck, in the spirit of diagnose_request.

It runs the same reviewed builders the individual tools use (job_progress,
job_material_shortages, wip_pending_transactions) plus a header lookup and a
per-job uncosted-transaction check, then decides the findings in Python
(diagnose_job_findings) so the model narrates a verdict rather than
inferring one from five result sets.

organization_code is required: job names are only unique within an
organization, and every follow-up query keys off the header's
WIP_ENTITY_ID, which is derived inside the caller's entitled scope.

Confidence: composed from the builders in wip.py, whose assumptions (status
codes, interface tables, error text join) apply here unchanged and are NOT
yet verified against a live instance. The per-job uncosted check assumes
MTL_MATERIAL_TRANSACTIONS.TRANSACTION_SOURCE_TYPE_ID = 5 is "job or
schedule". Findings are what these tables show — an empty list means none of
these checks fired, not that the job is healthy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ebsmcp.tools.manufacturing.wip import (
    JOB_STATUS_LABELS,
    build_job_progress_query,
    build_material_shortages_query,
    build_pending_transactions_query,
)
from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import MANUFACTURING_DOMAINS, ROW_CAP, inventory_org_filter

_TERMINAL = {7, 12}


def build_job_header_query(
    org_ids: tuple[str, ...],
    organization_code: str,
    job_name: str,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One row for the job, or none."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("we.wip_entity_name = :job_name")
    binds["job_name"] = job_name
    sql = (
        "SELECT we.wip_entity_id, wdj.organization_id, ood.organization_code, "
        "we.wip_entity_name AS job_name, msi.segment1 AS assembly, "
        "wdj.status_type, wdj.start_quantity, wdj.quantity_completed, "
        "wdj.quantity_scrapped, wdj.scheduled_start_date, wdj.scheduled_completion_date, "
        "wdj.date_released, wdj.date_completed "
        "FROM APPS.WIP_DISCRETE_JOBS wdj "
        "JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wdj.wip_entity_id "
        "AND we.organization_id = wdj.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = wdj.organization_id "
        "LEFT JOIN APPS.MTL_SYSTEM_ITEMS_B msi "
        "ON msi.inventory_item_id = wdj.primary_item_id "
        "AND msi.organization_id = wdj.organization_id "
        f"WHERE {' AND '.join(where)}"
    )
    return sql, binds


def build_job_uncosted_query(
    wip_entity_id: int,
    organization_id: int,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Takes ids lifted from the
    already-scoped header row, never caller input."""
    sql = (
        "SELECT mtt.transaction_type_name, mmt.costed_flag, mmt.error_code, "
        "mmt.error_explanation, COUNT(*) AS transaction_count "
        "FROM APPS.MTL_MATERIAL_TRANSACTIONS mmt "
        "JOIN APPS.MTL_TRANSACTION_TYPES mtt ON mtt.transaction_type_id = mmt.transaction_type_id "
        "WHERE mmt.transaction_source_type_id = 5 "
        "AND mmt.transaction_source_id = :wip_entity_id "
        "AND mmt.organization_id = :organization_id "
        "AND mmt.costed_flag IN ('N', 'E') "
        "GROUP BY mtt.transaction_type_name, mmt.costed_flag, mmt.error_code, "
        "mmt.error_explanation "
        "ORDER BY transaction_count DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, {"wip_entity_id": wip_entity_id, "organization_id": organization_id}


def _days_between(later: datetime, earlier: Any) -> int | None:
    if not isinstance(earlier, datetime):
        return None
    return (later - earlier).days


def diagnose_job_findings(
    header: dict,
    operations: list[dict],
    shortages: list[dict],
    move_errors: list[dict],
    resource_errors: list[dict],
    uncosted: list[dict],
    now: datetime,
) -> dict:
    """Pure function, unit-tested directly. Turns the raw result sets into
    an ordered list of findings (most blocking first) plus the status
    label. Severity: blocking (stops progress), warning (needs attention),
    info (context)."""
    status = header.get("status_type")
    findings: list[dict] = []

    def add(severity: str, code: str, message: str) -> None:
        findings.append({"severity": severity, "code": code, "message": message})

    if status in _TERMINAL:
        add("info", "terminal_status",
            f"Job is {JOB_STATUS_LABELS[status]}; no further shop-floor activity is expected.")
    elif status == 6:
        add("blocking", "on_hold", "Job is On Hold; transactions are blocked until it is released.")
    elif status == 1:
        add("blocking", "unreleased",
            "Job is Unreleased; no moves, issues or charges can be entered until it is released.")
    elif status in (14, 15):
        add("blocking", "close_problem",
            f"Job is {JOB_STATUS_LABELS[status]}; a close attempt did not complete.")

    if shortages:
        earliest = min((s.get("date_required") for s in shortages if s.get("date_required")),
                       default=None)
        add("blocking", "material_shortage",
            f"{len(shortages)} component requirement(s) exceed on-hand"
            + (f"; earliest needed {earliest:%Y-%m-%d}." if isinstance(earliest, datetime) else "."))

    for label, rows, code in (
        ("move", move_errors, "move_transaction_error"),
        ("resource", resource_errors, "resource_transaction_error"),
    ):
        if rows:
            first = rows[0].get("error_message") or "no error text recorded"
            add("blocking", code,
                f"{len(rows)} {label} transaction(s) stuck in the WIP interface; first error: {first}")

    if uncosted:
        total = sum(int(u.get("transaction_count") or 0) for u in uncosted)
        add("warning", "uncosted_transactions",
            f"{total} material transaction(s) for this job are not yet costed.")

    if operations and status == 3:
        last = max(operations, key=lambda o: o.get("operation_seq_num") or 0)
        waiting = last.get("quantity_waiting_to_move") or 0
        if waiting > 0:
            add("warning", "awaiting_completion",
                f"{waiting} unit(s) sit at the last operation ({last.get('operation_seq_num')}) "
                "waiting to move; a completion transaction has not been entered.")
        total_open = sum(
            (o.get("quantity_in_queue") or 0) + (o.get("quantity_running") or 0) for o in operations
        )
        if total_open == 0 and waiting == 0 and not (header.get("quantity_completed") or 0):
            add("warning", "no_quantity_in_process",
                "No quantity is in queue, running or waiting to move, and nothing is "
                "completed; the job may never have been started.")

    if status in (1, 3, 6):
        late = _days_between(now, header.get("scheduled_completion_date"))
        if late is not None and late > 0:
            add("warning", "past_schedule", f"Job is {late} day(s) past its scheduled completion.")

    order = {"blocking": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {
        "status": JOB_STATUS_LABELS.get(status, f"Unknown ({status})"),
        "blocked": any(f["severity"] == "blocking" for f in findings),
        "findings": findings,
    }


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True
        )
    )
    def diagnose_job(
        job_name: str,
        organization_code: str,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Explain why one discrete job is stuck, in a single call. Checks
        the job's status, component shortages against on-hand, move and
        resource transactions stuck in the WIP interface (with error text),
        quantity at each operation, uncosted material transactions and
        schedule slippage, and returns ordered findings (blocking, warning,
        info) with a blocked verdict. An empty findings list means none of
        those checks fired. organization_code is required because job names
        repeat across organizations. For fleet-wide status use
        wip_job_status. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"job_name": job_name, "organization_code": organization_code, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="diagnose_job", target_system="ebs", domains=MANUFACTURING_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_job_header_query(effective_org_ids, organization_code, job_name)
            header_rows = connector.run(sql, binds)
            if not header_rows:
                raise ToolError(
                    f"No job {job_name!r} found in organization {organization_code!r} "
                    "within the caller's permitted operating units."
                )
            header = header_rows[0]

            def run(builder_result: tuple[str, dict[str, Any]]) -> list[dict]:
                rows, _ = split_total_count(connector.run(*builder_result))
                return rows

            operations = run(build_job_progress_query(effective_org_ids, organization_code, job_name))
            shortages = run(
                build_material_shortages_query(effective_org_ids, organization_code, job_name)
            )
            move_errors = run(
                build_pending_transactions_query(
                    effective_org_ids, organization_code, "move", False, job_name
                )
            )
            resource_errors = run(
                build_pending_transactions_query(
                    effective_org_ids, organization_code, "resource", False, job_name
                )
            )
            uncosted = connector.run(
                *build_job_uncosted_query(header["wip_entity_id"], header["organization_id"])
            )

            diagnosis = diagnose_job_findings(
                header, operations, shortages, move_errors, resource_errors, uncosted,
                datetime.now(),
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "job": {
                    k: header.get(k)
                    for k in (
                        "job_name", "organization_code", "assembly", "start_quantity",
                        "quantity_completed", "quantity_scrapped", "scheduled_start_date",
                        "scheduled_completion_date", "date_released", "date_completed",
                    )
                },
                **diagnosis,
                "operations": operations,
                "shortages": shortages,
                "move_errors": move_errors,
                "resource_errors": resource_errors,
                "uncosted": uncosted,
            }


DIAGNOSE_JOB_TOOLSET = ToolSet(name="mfg_diagnose_job", register=_register)
