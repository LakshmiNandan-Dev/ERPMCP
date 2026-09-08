"""diagnose_request: the catalog's flagship composed tool — why a
specific concurrent request is slow, not just that it is. Correlates
several atomic sources the way a DBA would by running several queries in
sequence and connecting the results by hand, per the architecture doc's
6-step description (section 09).

Confidence varies sharply by step — flagged inline, not glossed over:
- Request lookup, historical comparison: HIGH, same
  FND_CONCURRENT_REQUESTS/FND_CONCURRENT_PROGRAMS(_TL) footing as
  concurrent_processing.py's other tools.
- Session resolution (step 3): LOW, the weakest link in this tool.
  FND_CONCURRENT_REQUESTS.OS_PROCESS_ID is the concurrent manager's own
  OS process, not necessarily a 1:1 live DB session for every program
  type — some programs share a manager's persistent connection rather
  than opening their own. This queries GV$SESSION by MODULE (the more
  commonly-cited EBS convention for this correlation) OR PROCESS as a
  best-effort fallback; neither is asserted as definitely correct
  without a real instance to verify against.
- Live diagnostics (step 4): MEDIUM — GV$SESSION_LONGOPS/GV$SQL columns
  are standard, but only pulls sql_id/sql_text here, not the full
  plan-tree/bind/cardinality treatment sql_plan_detail will eventually
  own as its own tool.
- Likely-causes (step 6): stale-statistics correlation is deliberately
  NOT attempted here — it depends on the referenced-table list a real
  sql_plan_detail tool would supply, which is genuinely that tool's job,
  not this one's to duplicate ahead of it existing.

Excludes GV$ACTIVE_SESSION_HISTORY/DBA_HIST_* per the doc's own licensing
note (Diagnostic Pack) — see connectors/base.py's SQL conventions for the
GV$/schema-qualification rules every query here still follows.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

_REQUEST_LOOKUP_SQL = (
    "SELECT fcr.request_id, fcr.phase_code, fcr.status_code, fcr.actual_start_date, "
    "ROUND((SYSDATE - fcr.actual_start_date) * 24 * 60, 1) AS elapsed_minutes, "
    "fcpt.user_concurrent_program_name AS program_name, "
    "fcp.concurrent_program_name AS program_short_name, "
    "fcr.os_process_id, fcr.concurrent_program_id, fcr.program_application_id "
    "FROM APPLSYS.FND_CONCURRENT_REQUESTS fcr "
    "JOIN APPLSYS.FND_CONCURRENT_PROGRAMS fcp "
    "  ON fcp.application_id = fcr.program_application_id "
    " AND fcp.concurrent_program_id = fcr.concurrent_program_id "
    "JOIN APPLSYS.FND_CONCURRENT_PROGRAMS_TL fcpt "
    "  ON fcpt.application_id = fcr.program_application_id "
    " AND fcpt.concurrent_program_id = fcr.concurrent_program_id "
    " AND fcpt.language = 'US' "
    "WHERE fcr.request_id = :request_id"
)

# Best-effort session correlation — see module docstring's step-3 caveat.
_SESSION_LOOKUP_SQL = (
    "SELECT gs.sid, gs.serial#, gs.inst_id, gs.status, gs.event, gs.wait_class, "
    "gs.blocking_session, gs.sql_id, gs.module "
    "FROM GV$SESSION gs "
    "WHERE gs.module = :program_short_name OR gs.process = :os_process_id"
)

_LONGOPS_SQL = (
    "SELECT slo.sid, slo.serial#, slo.opname, slo.target, slo.sofar, slo.totalwork, slo.units, "
    "ROUND(slo.sofar / NULLIF(slo.totalwork, 0) * 100, 1) AS pct_complete "
    "FROM GV$SESSION_LONGOPS slo "
    "WHERE slo.sid = :sid AND slo.serial# = :serial_number AND slo.sofar < slo.totalwork"
)

_CURRENT_SQL_SQL = (
    "SELECT sq.sql_id, sq.sql_text "
    "FROM GV$SQL sq "
    "WHERE sq.sql_id = :sql_id AND sq.inst_id = :inst_id "
    "FETCH FIRST 1 ROW ONLY"
)

_HISTORY_SQL = (
    "SELECT AVG((fcr.actual_completion_date - fcr.actual_start_date) * 24 * 60) AS avg_minutes, "
    "MEDIAN((fcr.actual_completion_date - fcr.actual_start_date) * 24 * 60) AS median_minutes "
    "FROM APPLSYS.FND_CONCURRENT_REQUESTS fcr "
    "WHERE fcr.concurrent_program_id = :concurrent_program_id "
    "  AND fcr.program_application_id = :program_application_id "
    "  AND fcr.phase_code = 'C' AND fcr.status_code = 'C' "
    "  AND fcr.actual_completion_date >= SYSDATE - 30"
)


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def diagnose_request(request_id: int, instance: str | None = None) -> dict:
        """Diagnose why a specific concurrent request is slow, not just
        that it is: resolves its live Oracle session, current SQL,
        percent-complete, any blocker, and compares elapsed time against
        this program's own historical average. If the request isn't
        actually running yet (Pending/Standby), reports that the cause
        is scheduling instead of chasing a session that doesn't exist.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx, tool_name="diagnose_request", target_system="ebs_dba", params={"request_id": request_id},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            request_rows = connector.run(_REQUEST_LOOKUP_SQL, {"request_id": request_id})
            if not request_rows:
                raise ToolError(f"No concurrent request found with request_id={request_id}.")
            request = request_rows[0]

            base: dict[str, Any] = {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "request_id": request_id,
                "program_name": request["program_name"],
                "phase_code": request["phase_code"],
                "status_code": request["status_code"],
                "elapsed_minutes": request["elapsed_minutes"],
            }

            if request["phase_code"] != "R":
                return {
                    **base,
                    "diagnosis": "not_running",
                    "detail": (
                        "Request is not in the Running phase — the cause is scheduling "
                        "(manager capacity, or waiting on an incompatible program), not SQL "
                        "performance. No Oracle session to inspect."
                    ),
                }

            session_rows = connector.run(
                _SESSION_LOOKUP_SQL,
                {
                    "program_short_name": request["program_short_name"],
                    "os_process_id": request["os_process_id"],
                },
            )
            if not session_rows:
                return {
                    **base,
                    "diagnosis": "session_not_found",
                    "detail": (
                        "Request is Running but no matching GV$SESSION row was found — it may "
                        "have just started or completed, or the module/process correlation "
                        "didn't match this time (see diagnose_request's module docstring)."
                    ),
                }
            session = session_rows[0]

            longops_rows = connector.run(
                _LONGOPS_SQL, {"sid": session["sid"], "serial_number": session["serial#"]}
            )
            current_sql_rows = (
                connector.run(_CURRENT_SQL_SQL, {"sql_id": session["sql_id"], "inst_id": session["inst_id"]})
                if session.get("sql_id")
                else []
            )

            history_rows = connector.run(
                _HISTORY_SQL,
                {
                    "concurrent_program_id": request["concurrent_program_id"],
                    "program_application_id": request["program_application_id"],
                },
            )
            history = history_rows[0] if history_rows else {}

            causes: list[str] = []
            if session.get("blocking_session") is not None:
                causes.append(f"Blocked by session {session['blocking_session']}.")
            avg_minutes = history.get("avg_minutes")
            elapsed_minutes = request["elapsed_minutes"]
            if avg_minutes and elapsed_minutes and elapsed_minutes > 2 * avg_minutes:
                causes.append(
                    f"Running {elapsed_minutes:.1f} min vs. a {avg_minutes:.1f} min "
                    "historical average for this program."
                )
            if not causes:
                causes.append("No blocker detected and elapsed time is within historical norms so far.")

            return {
                **base,
                "diagnosis": "running",
                "session": {
                    "sid": session["sid"],
                    "serial#": session["serial#"],
                    "status": session["status"],
                    "event": session["event"],
                    "wait_class": session["wait_class"],
                    "blocking_session": session.get("blocking_session"),
                },
                "current_sql": current_sql_rows[0] if current_sql_rows else None,
                "longops": longops_rows,
                "history": history,
                "likely_causes": causes,
            }


DIAGNOSE_REQUEST_TOOLSET = ToolSet(name="diagnose_request", register=_register)
