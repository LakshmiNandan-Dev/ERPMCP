"""Output/printing health: three genuinely distinct tools, no
consolidation opportunity here — each reads a different source
(registered printers, the OPP queue, and completed-but-outputless
requests), unlike categories with real near-duplicate filters to merge.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def printer_registration_status(instance: str | None = None) -> dict:
        """List every printer registered in EBS — the DBA's starting
        point for verifying a given printer is actually reachable from a
        concurrent manager node. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="printer_registration_status", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            # Verified against a live instance (2026-09-02): the table is
            # FND_PRINTER, not FND_PRINTER_INFO (ORA-00942 — that table
            # doesn't exist), and FND_PRINTER only has PRINTER_NAME/
            # PRINTER_TYPE — no print-command column. The actual OS print
            # command lives at the driver level (FND_PRINTER_DRIVERS,
            # keyed by printer_type), a deeper join not chased here.
            rows = connector.run(
                "SELECT fp.printer_name, fp.printer_type "
                "FROM APPS.FND_PRINTER fp "
                "ORDER BY fp.printer_name"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "printers": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def opp_status(instance: str | None = None) -> dict:
        """Report the Output Post Processor queue's activation state —
        when OPP stalls, requests complete Normal but produce no output,
        so this is the first thing to check for that symptom. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="opp_status", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            # Filtering by fcq.concurrent_queue_name (the internal short
            # name) turned out edition-fragile on a live instance
            # (2026-09-02): that column showed up suffixed
            # CONCURRENT_QUEUE_NAME#1, an ADOP/edition-in-flux artifact,
            # not a stable name to filter on. The _TL display name is the
            # stable filter instead — same join shape as manager_status,
            # just narrowed to one manager.
            rows = connector.run(
                "SELECT fcq.concurrent_queue_id, fcqt.user_concurrent_queue_name AS manager_name, "
                "fcq.running_processes, fcq.max_processes, fcq.enabled_flag, fcq.control_code "
                "FROM APPS.FND_CONCURRENT_QUEUES fcq "
                "JOIN APPS.FND_CONCURRENT_QUEUES_TL fcqt "
                "  ON fcqt.application_id = fcq.application_id "
                " AND fcqt.concurrent_queue_id = fcq.concurrent_queue_id "
                " AND fcqt.language = 'US' "
                "WHERE fcqt.user_concurrent_queue_name = 'Output Post Processor'"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "opp_queue": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def output_file_errors(instance: str | None = None) -> dict:
        """List concurrent requests from the last 24 hours that completed
        successfully, were flagged for printing, but have no output file
        — a likely sign the print pipeline is failing. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="output_file_errors", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            # PRINT_FLAG doesn't exist on FND_CONCURRENT_REQUESTS —
            # verified against a live instance (2026-09-02), ORA-00904.
            # SAVE_OUTPUT_FLAG (a real column) is the closest available
            # signal for "this request's output was meant to be kept."
            rows = connector.run(
                "SELECT fcr.request_id, "
                "fcpt.user_concurrent_program_name AS program_name, "
                "fcr.status_code, fcr.actual_completion_date "
                "FROM APPS.FND_CONCURRENT_REQUESTS fcr "
                "JOIN APPS.FND_CONCURRENT_PROGRAMS_TL fcpt "
                "  ON fcpt.application_id = fcr.program_application_id "
                " AND fcpt.concurrent_program_id = fcr.concurrent_program_id "
                " AND fcpt.language = 'US' "
                "WHERE fcr.status_code = 'C' "
                "  AND fcr.save_output_flag = 'Y' "
                "  AND fcr.output_file_type IS NULL "
                "  AND fcr.actual_completion_date > SYSDATE - 1 "
                "ORDER BY fcr.actual_completion_date DESC"
            )

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "requests": rows,
            }


OUTPUT_PRINTING_TOOLSET = ToolSet(name="output_printing", register=_register)
