"""Concurrent Manager health: one parameterized tool over
FND_CONCURRENT_REQUESTS, not a separate tool per filter.

A wall of near-identical single-purpose tools (long_running_requests,
failed_concurrent_requests, requests_by_user, requests_on_hold, ...) is
exactly the selection-accuracy risk tools/registry.py's own docstring
warns about ("sixty tools in one flat MCP server... the fix isn't better
tool descriptions, it's not exposing all sixty at once") — recreated one
category at a time instead of once. One tool with a status enum plus
optional filters covers the same query space under one name.

_STATUS_CONDITIONS' (phase_code, status_code) pairs are the same
well-documented values used throughout EBS DBA tooling: 'R'/'C'/'P' phase
codes, 'C' (Normal)/'E' (Error)/'H' (Hold) status codes.

Only the status enum (a closed, code-reviewed set of WHERE fragments)
varies the query's *shape* — requested_by/since are always passed as bind
variables, never string-interpolated, so an LLM-constructed call can
select which filter applies but never inject SQL, the same principle
policy/entitlement.py states for Org IDs: caller-supplied values narrow,
they never author query structure.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count

RequestStatus = Literal["running", "pending", "on_hold", "completed", "failed"]

_STATUS_CONDITIONS: dict[RequestStatus, str] = {
    "running": "fcr.phase_code = 'R'",
    "pending": "fcr.phase_code = 'P'",
    "on_hold": "fcr.phase_code = 'P' AND fcr.status_code = 'H'",
    "completed": "fcr.phase_code = 'C' AND fcr.status_code = 'C'",
    "failed": "fcr.phase_code = 'C' AND fcr.status_code = 'E'",
}

# completed/failed requests are filtered by when they finished; anything
# still pending/running/on_hold hasn't finished yet, so "since" means when
# it started instead.
_COMPLETION_STATUSES: frozenset[RequestStatus] = frozenset({"completed", "failed"})


def build_query(
    status: RequestStatus,
    requested_by: str | None,
    since: str | None,
    program_name: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly in
    tests/test_concurrent_requests_query.py — no need to exercise every
    status/filter combination through the full MCP protocol when the
    thing that actually varies is this WHERE-clause/bind construction.

    program_name (case-insensitive, exact match against the display
    name) plus since (a plain YYYY-MM-DD floor, not a rolling window —
    the caller computes "last 24h"/"last week"/"last month" as a date
    from today, same as every other since-style filter in this catalog)
    together answer "how has program X been running over the last
    day/week/month" — combined with status="completed" and read
    alongside the summary summarize_concurrent_requests() builds from
    the returned elapsed_minutes values.

    Session correlation (sid/serial#/session_status/sql_id/wait_event) —
    same best-effort technique diagnose_request.py already uses and flags
    as LOW confidence — is added ONLY for status="running". Two reasons,
    not one: (1) performance, verified live (2026-09-02) — joining
    GV$SESSION unconditionally made "completed" hang past 30s with no
    since filter, since Oracle can't push FETCH FIRST past a LEFT JOIN
    against a dynamic view cheaply once FND_CONCURRENT_REQUESTS' historical
    row count is large; "running" alone stayed at ~300ms because so few
    rows ever match phase_code='R'. (2) correctness — a "session match"
    against a request that finished hours or days ago is actively
    misleading, not just slow: OS process IDs and MODULE strings get
    reused by unrelated later sessions, so joining live GV$SESSION against
    old completed/failed/pending rows would sometimes attach a real-
    looking but wrong session to history. Every status still gets
    elapsed_minutes (cheap row arithmetic, no join) and the same 5 session
    columns in its output shape — literal NULLs for non-running statuses,
    not a differently-shaped result depending on status. Capped at 50
    rows — same reasoning as every other list-shaped tool in this catalog
    after the active_sessions incident (2026-09-02): an unbounded
    "completed" query with no since filter has no natural upper bound.
    """
    where = [_STATUS_CONDITIONS[status]]
    binds: dict[str, Any] = {}

    if requested_by:
        where.append("fu.user_name = :requested_by")
        # EBS usernames are conventionally stored upper-case (FND_USER.USER_NAME).
        binds["requested_by"] = requested_by.upper()

    if since:
        date_column = "fcr.actual_completion_date" if status in _COMPLETION_STATUSES else "fcr.actual_start_date"
        where.append(f"{date_column} >= TO_DATE(:since, 'YYYY-MM-DD')")
        binds["since"] = since

    if program_name:
        # Case-insensitive: EBS program display names are mixed-case
        # (e.g. "Create Accounting"), unlike FND_USER.USER_NAME.
        where.append("UPPER(fcpt.user_concurrent_program_name) = UPPER(:program_name)")
        binds["program_name"] = program_name

    if status == "running":
        program_join = (
            "JOIN APPS.FND_CONCURRENT_PROGRAMS fcp "
            "  ON fcp.application_id = fcr.program_application_id "
            " AND fcp.concurrent_program_id = fcr.concurrent_program_id "
        )
        session_join = (
            "LEFT JOIN GV$SESSION gs "
            "  ON gs.module = fcp.concurrent_program_name OR gs.process = fcr.os_process_id "
        )
        session_columns = "gs.sid, gs.serial#, gs.status AS session_status, gs.sql_id, gs.event AS wait_event "
    else:
        program_join = ""
        session_join = ""
        session_columns = (
            "NULL AS sid, NULL AS serial#, NULL AS session_status, NULL AS sql_id, NULL AS wait_event "
        )

    sql = (
        "SELECT fcr.request_id, fcpt.user_concurrent_program_name AS program_name, "
        "fcr.phase_code, fcr.status_code, fcr.actual_start_date, fcr.actual_completion_date, "
        "ROUND((NVL(fcr.actual_completion_date, SYSDATE) - fcr.actual_start_date) * 24 * 60, 1) "
        "AS elapsed_minutes, "
        "fu.user_name AS requested_by, COUNT(*) OVER () AS total_count, "
        f"{session_columns}"
        "FROM APPS.FND_CONCURRENT_REQUESTS fcr "
        f"{program_join}"
        "JOIN APPS.FND_CONCURRENT_PROGRAMS_TL fcpt "
        "  ON fcpt.application_id = fcr.program_application_id "
        " AND fcpt.concurrent_program_id = fcr.concurrent_program_id "
        " AND fcpt.language = 'US' "
        "JOIN APPS.FND_USER fu ON fu.user_id = fcr.requested_by "
        f"{session_join}"
        f"WHERE {' AND '.join(where)} "
        "ORDER BY fcr.actual_start_date DESC "
        "FETCH FIRST 50 ROWS ONLY"
    )
    return sql, binds


def summarize_concurrent_requests(
    rows: list[dict], status: RequestStatus, total: int | None = None
) -> str:
    """Turns a page of individual rows into the one-line answer "how has
    this actually been running" — count plus avg/min/max elapsed_minutes
    among whatever rows came back. Computed in Python from the already-
    fetched rows, not a second SQL round trip — same reasoning as
    instance_health.py's annotate_* helpers. Pending requests (no
    elapsed_minutes yet) are counted but excluded from the duration
    stats rather than treated as 0-minute runs.

    Pure function, unit-tested directly — see
    test_concurrent_requests_query.py.
    """
    if not rows:
        return f"No {status} requests found"

    # Say so when the cap hid rows. "50 failed request(s)" read as the whole
    # picture when 688 matched — the count a DBA acts on has to be the real
    # one, and the stats below describe only the page that came back.
    shown = f"Showing {len(rows)} of {total} " if total is not None and total > len(rows) else ""

    durations = [r["elapsed_minutes"] for r in rows if r.get("elapsed_minutes") is not None]
    if not durations:
        if shown:
            return f"{shown}{status} request(s) (no elapsed time available yet)"
        return f"{len(rows)} {status} request(s) found (no elapsed time available yet)"

    avg = sum(durations) / len(durations)
    if shown:
        return (
            f"{shown}{status} request(s) — of the {len(rows)} shown: "
            f"avg {avg:.1f} min, min {min(durations):.1f} min, max {max(durations):.1f} min"
        )
    return (
        f"{len(rows)} {status} request(s) — "
        f"avg {avg:.1f} min, min {min(durations):.1f} min, max {max(durations):.1f} min"
    )


ManagerCapacityView = Literal["target_vs_actual", "by_worker"]

_MANAGER_CAPACITY_QUERIES: dict[ManagerCapacityView, str] = {
    "target_vs_actual": (
        "SELECT fcq.concurrent_queue_id, fcqt.user_concurrent_queue_name AS manager_name, "
        "fcq.max_processes AS target_processes, "
        "COUNT(fcp.concurrent_process_id) AS actual_processes "
        "FROM APPS.FND_CONCURRENT_QUEUES fcq "
        "JOIN APPS.FND_CONCURRENT_QUEUES_TL fcqt "
        "  ON fcqt.application_id = fcq.application_id "
        " AND fcqt.concurrent_queue_id = fcq.concurrent_queue_id "
        " AND fcqt.language = 'US' "
        "LEFT JOIN APPS.FND_CONCURRENT_PROCESSES fcp "
        "  ON fcp.concurrent_queue_id = fcq.concurrent_queue_id "
        " AND fcp.queue_application_id = fcq.application_id "
        " AND fcp.process_status_code = 'A' "
        "GROUP BY fcq.concurrent_queue_id, fcqt.user_concurrent_queue_name, fcq.max_processes "
        "ORDER BY fcqt.user_concurrent_queue_name"
    ),
    "by_worker": (
        "SELECT fcp.concurrent_queue_id, fcqt.user_concurrent_queue_name AS manager_name, "
        "fcp.concurrent_process_id, fcp.process_status_code, fcp.oracle_process_id "
        "FROM APPS.FND_CONCURRENT_PROCESSES fcp "
        "JOIN APPS.FND_CONCURRENT_QUEUES_TL fcqt "
        "  ON fcqt.application_id = fcp.queue_application_id "
        " AND fcqt.concurrent_queue_id = fcp.concurrent_queue_id "
        " AND fcqt.language = 'US' "
        "ORDER BY fcqt.user_concurrent_queue_name, fcp.concurrent_process_id"
    ),
}


def get_manager_capacity_query(view: ManagerCapacityView) -> str:
    """Pure function, unit-tested directly — see
    test_concurrent_processing_queries.py."""
    return _MANAGER_CAPACITY_QUERIES[view]


LoadTrendGroupBy = Literal["program", "hour"]

_LOAD_TREND_QUERIES: dict[LoadTrendGroupBy, str] = {
    "program": (
        "SELECT fcpt.user_concurrent_program_name AS program_name, "
        "COUNT(*) AS request_count, "
        "SUM(CASE WHEN fcr.phase_code = 'R' THEN 1 ELSE 0 END) AS running_count, "
        "SUM(CASE WHEN fcr.phase_code = 'P' THEN 1 ELSE 0 END) AS pending_count "
        "FROM APPS.FND_CONCURRENT_REQUESTS fcr "
        "JOIN APPS.FND_CONCURRENT_PROGRAMS_TL fcpt "
        "  ON fcpt.application_id = fcr.program_application_id "
        " AND fcpt.concurrent_program_id = fcr.concurrent_program_id "
        " AND fcpt.language = 'US' "
        "WHERE fcr.phase_code IN ('R', 'P') "
        "GROUP BY fcpt.user_concurrent_program_name "
        "ORDER BY request_count DESC"
    ),
    "hour": (
        "SELECT TRUNC(fcr.actual_completion_date, 'HH24') AS completion_hour, "
        "COUNT(*) AS completed_count "
        "FROM APPS.FND_CONCURRENT_REQUESTS fcr "
        "WHERE fcr.actual_completion_date >= SYSDATE - 1 "
        "GROUP BY TRUNC(fcr.actual_completion_date, 'HH24') "
        "ORDER BY completion_hour"
    ),
}


def get_load_trend_query(group_by: LoadTrendGroupBy) -> str:
    """Pure function, unit-tested directly — see
    test_concurrent_processing_queries.py."""
    return _LOAD_TREND_QUERIES[group_by]


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def concurrent_requests(
        status: RequestStatus = "running",
        requested_by: str | None = None,
        since: str | None = None,
        program_name: str | None = None,
        instance: str | None = None,
    ) -> dict:
        """List concurrent requests matching status (running, pending,
        on_hold, completed, or failed), optionally narrowed to one
        requestor's EBS username, one program (program_name — case-
        insensitive, e.g. "Create Accounting"), and/or a start date
        (since, YYYY-MM-DD — filters by completion date for
        completed/failed status, by start date otherwise). For "how has
        program X been running over the last day/week/month", pass
        program_name with status="completed" and since set to that many
        days back from today — the summary reports count plus avg/min/
        max elapsed_minutes across whatever came back. Defaults to
        running (no program_name), the standard first check for a stuck
        or runaway concurrent program. Every row includes
        elapsed_minutes; for status="running" only, also that request's
        live Oracle session (best-effort correlation — see source) —
        sid, serial#, session_status, currently-executing sql_id, and
        wait_event, the drill-down for "why is this actually stuck."
        Other statuses report those 5 fields as null: a session "match"
        against a long-finished request would be coincidental, not
        real. Capped at 50 rows, most recently started first.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx,
            tool_name="concurrent_requests",
            target_system="ebs_dba",
            params={
                "status": status,
                "requested_by": requested_by,
                "since": since,
                "program_name": program_name,
            },
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_query(status, requested_by, since, program_name)
            rows, total = split_total_count(connector.run(sql, binds))

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "filters": {
                    "status": status,
                    "requested_by": requested_by,
                    "since": since,
                    "program_name": program_name,
                },
                "total_count": total,
                "summary": summarize_concurrent_requests(rows, status, total),
                "requests": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def manager_status(instance: str | None = None) -> dict:
        """Report every concurrent manager's activation state: running
        vs. max processes, enabled flag, and raw control code. The
        standard first check for whether concurrent managers are up. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="manager_status", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT fcq.concurrent_queue_id, fcqt.user_concurrent_queue_name AS manager_name, "
                "fcq.running_processes, fcq.max_processes, fcq.enabled_flag, fcq.control_code "
                "FROM APPS.FND_CONCURRENT_QUEUES fcq "
                "JOIN APPS.FND_CONCURRENT_QUEUES_TL fcqt "
                "  ON fcqt.application_id = fcq.application_id "
                " AND fcqt.concurrent_queue_id = fcq.concurrent_queue_id "
                " AND fcqt.language = 'US' "
                "ORDER BY fcqt.user_concurrent_queue_name"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "managers": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def manager_capacity(view: ManagerCapacityView = "target_vs_actual", instance: str | None = None) -> dict:
        """Report manager capacity: target vs. actual process count per
        manager (target_vs_actual), or a per-worker-process breakdown
        (by_worker) — distinguishes an under-resourced manager from one
        genuinely slow request. Defaults to target_vs_actual. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="manager_capacity", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_manager_capacity_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def concurrent_load_trend(group_by: LoadTrendGroupBy = "program", instance: str | None = None) -> dict:
        """Report concurrent request load, grouped either by program
        (which program is generating the most load right now, running +
        pending) or by hour (completed-per-hour over the last 24 hours,
        catching throughput quietly degrading). Defaults to program. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="concurrent_load_trend", target_system="ebs_dba", params={"group_by": group_by},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_load_trend_query(group_by))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "group_by": group_by,
                "results": rows,
            }


CONCURRENT_PROCESSING_TOOLSET = ToolSet(name="concurrent_processing", register=_register)
