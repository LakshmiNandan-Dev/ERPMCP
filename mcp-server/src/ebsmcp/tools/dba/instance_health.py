"""Instance health: the largest DBA category — 22 tools alongside
the original instance_status, all reading standard DBA_*/GV$* dictionary
and dynamic-performance views.

object_errors (DBA_ERRORS) is deliberately paired with database_objects
(DBA_OBJECTS): status alone can't distinguish an object merely
invalidated by a dependency change from one that genuinely failed to
compile, and that distinction is the whole post-patch triage question. This is the most textbook-Oracle-DBA
category built so far (no EBS-specific FND/WF/AD schema guessing), so
confidence runs higher across the board — HIGH unless noted otherwise
below.

init_parameters, undo_segment_detail, open_cursors, object_lock_inventory,
recyclebin, dangling_synonyms, and license_and_options were added from
the oracle-base.com/dba/scripts Monitoring inventory (2026-09-02) — all
standard Oracle dictionary/dynamic-performance views, columns verified
against a live instance before writing the SQL, not guessed then fixed.

backup_status is NOT duplicated here even though the catalog lists it
under this category too — it already lives in redo_archive_backup.py,
per the catalog's own cross-reference note ("already in the catalog
above").

Lower-confidence exceptions:
- temp_usage_by_session: MEDIUM — GV$TEMPSEG_USAGE is real but less
  commonly used than the other views here; block size is hardcoded to
  8192 rather than derived from DBA_TABLESPACES.BLOCK_SIZE, a real
  simplification for a "roughly how much" figure.
- alert_log_errors: MEDIUM-HIGH — GV$DIAG_ALERT_EXT is a real, ADR-based
  view (11g+) but more specialized than the others in this batch.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import (
    ToolContext,
    ToolSet,
    permitted_instances,
    resolve_identity_only,
    resolve_scoped_call,
)

TablespaceView = Literal["usage", "datafile_headroom", "status"]

_TABLESPACE_QUERIES: dict[TablespaceView, str] = {
    "usage": (
        "SELECT d.tablespace_name, "
        "ROUND(d.allocated_bytes / 1024 / 1024, 1) AS allocated_mb, "
        "ROUND((d.allocated_bytes - NVL(f.free_bytes, 0)) / 1024 / 1024, 1) AS used_mb, "
        "ROUND(NVL(f.free_bytes, 0) / 1024 / 1024, 1) AS free_mb, "
        "ROUND((1 - NVL(f.free_bytes, 0) / d.allocated_bytes) * 100, 1) AS pct_used "
        "FROM (SELECT tablespace_name, SUM(bytes) AS allocated_bytes FROM DBA_DATA_FILES GROUP BY tablespace_name) d "
        "LEFT JOIN (SELECT tablespace_name, SUM(bytes) AS free_bytes FROM DBA_FREE_SPACE GROUP BY tablespace_name) f "
        "  ON f.tablespace_name = d.tablespace_name "
        "ORDER BY pct_used DESC"
    ),
    "datafile_headroom": (
        "SELECT file_name, tablespace_name, bytes, maxbytes, autoextensible, "
        "ROUND(bytes / NULLIF(maxbytes, 0) * 100, 1) AS pct_of_max "
        "FROM DBA_DATA_FILES "
        "WHERE autoextensible = 'YES' AND maxbytes > 0 "
        "ORDER BY pct_of_max DESC"
    ),
    "status": (
        "SELECT tablespace_name, status, contents, extent_management "
        "FROM DBA_TABLESPACES "
        "WHERE status != 'ONLINE' "
        "ORDER BY tablespace_name"
    ),
}


def get_tablespace_query(view: TablespaceView) -> str:
    return _TABLESPACE_QUERIES[view]


def annotate_tablespace_usage(rows: list[dict]) -> tuple[list[dict], str]:
    """Pilot for making DBA tool output easier for an assisting LLM (e.g.
    Copilot) to turn into a friendly answer: a per-row severity flag plus
    a one-line summary, alongside the raw rows — not a replacement for
    them. Thresholds (75%/90%) are the same ones any Oracle DBA reference
    uses for tablespace usage warnings.

    Pure function, unit-tested directly — see
    test_instance_health_enrichment.py.
    """
    annotated: list[dict] = []
    critical = warning = 0
    for row in rows:
        pct = row.get("pct_used")
        if pct is not None and pct >= 90:
            severity = "critical"
            critical += 1
        elif pct is not None and pct >= 75:
            severity = "warning"
            warning += 1
        else:
            severity = "ok"
        annotated.append({**row, "severity": severity})

    if critical:
        summary = f"{critical} tablespace(s) critical (>=90% used)"
        if warning:
            summary += f", {warning} at warning level (>=75%)"
    elif warning:
        summary = f"{warning} tablespace(s) at warning level (>=75% used)"
    else:
        summary = "All tablespaces healthy (below 75% used)"

    return annotated, summary


def annotate_blocking_locks(rows: list[dict]) -> tuple[list[dict], str]:
    """Same pilot as annotate_tablespace_usage, applied to a
    duration-based check instead of a percentage-based one. 60s/300s
    aren't from a specific Oracle reference — they're a reasonable
    "worth a glance" / "worth paging someone" split for a wait chain,
    same spirit as the pct thresholds above.

    Pure function, unit-tested directly — see
    test_instance_health_enrichment.py.
    """
    annotated: list[dict] = []
    for row in rows:
        seconds = row.get("seconds_in_wait")
        if seconds is not None and seconds >= 300:
            severity = "critical"
        elif seconds is not None and seconds >= 60:
            severity = "warning"
        else:
            severity = "ok"
        annotated.append({**row, "severity": severity})

    if not annotated:
        return annotated, "No blocking locks detected"

    waits = [r["seconds_in_wait"] for r in annotated if r.get("seconds_in_wait") is not None]
    summary = f"{len(annotated)} session(s) blocked"
    if waits:
        summary += f", longest wait {max(waits)}s"
    return annotated, summary


def build_database_objects_query(
    object_name: str | None,
    object_type: str | None,
    status: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly — see
    test_database_objects_query.py. Backs the database_objects tool
    (formerly invalid_objects, with status hardcoded to 'INVALID'): an
    optional lookup by name/type/status — status still defaults to
    'INVALID' at the tool level, so calling with no arguments is
    unchanged from before; pass status=None here for every status, or
    another value (e.g. 'VALID') to confirm a specific object
    recompiled successfully.
    """
    where: list[str] = []
    binds: dict[str, Any] = {}
    if object_name:
        where.append("object_name = :object_name")
        binds["object_name"] = object_name.upper()
    if object_type:
        where.append("object_type = :object_type")
        binds["object_type"] = object_type.upper()
    if status:
        where.append("status = :status")
        binds["status"] = status.upper()
    where_clause = f"WHERE {' AND '.join(where)} " if where else ""

    sql = (
        "SELECT owner, object_name, object_type, status, created, last_ddl_time, timestamp "
        "FROM DBA_OBJECTS "
        f"{where_clause}"
        "ORDER BY owner, object_name"
    )
    return sql, binds


ErrorAttribute = Literal["ERROR", "WARNING"]

# One broken package body can emit dozens of lines; without a cap a
# single bad patch object floods the whole response.
_OBJECT_ERRORS_ROW_CAP = 100


def build_object_errors_query(
    object_name: str | None,
    object_type: str | None,
    owner: str | None,
    attribute: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly — see
    test_object_errors_query.py. Same bind discipline as
    build_database_objects_query: every filter is bound and upper-cased,
    never interpolated.

    attribute defaults to 'ERROR' at the tool level because
    PLSQL_WARNINGS leaves WARNING rows behind on objects that compiled
    perfectly well — including them by default would bury the actual
    post-patch failures. Pass None here for both.
    """
    where: list[str] = []
    binds: dict[str, Any] = {}
    if object_name:
        where.append("name = :object_name")
        binds["object_name"] = object_name.upper()
    if object_type:
        where.append("type = :object_type")
        binds["object_type"] = object_type.upper()
    if owner:
        where.append("owner = :owner")
        binds["owner"] = owner.upper()
    if attribute:
        where.append("attribute = :attribute")
        binds["attribute"] = attribute.upper()
    where_clause = f"WHERE {' AND '.join(where)} " if where else ""

    sql = (
        "SELECT owner, name, type, sequence, line, position, attribute, text "
        "FROM DBA_ERRORS "
        f"{where_clause}"
        "ORDER BY owner, name, type, sequence "
        f"FETCH FIRST {_OBJECT_ERRORS_ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def summarize_object_errors(rows: list[dict], attribute: str | None) -> str:
    """The empty case carries the actual diagnostic value here: an object
    that is INVALID with no rows in DBA_ERRORS was invalidated by a
    dependency change, not broken — it recompiles on next reference or
    under utlrp. Saying so is what stops "no errors" being read as "the
    lookup failed".
    """
    if not rows:
        scope = f" with attribute={attribute.upper()}" if attribute else ""
        return (
            f"No compilation errors recorded{scope}. An object that is INVALID "
            "but has no errors here was invalidated by a dependency change "
            "rather than broken, and should recompile on next reference or "
            "under utlrp."
        )
    objects = {(row.get("owner"), row.get("name"), row.get("type")) for row in rows}
    summary = f"{len(objects)} object(s) with {len(rows)} error line(s)"
    if len(rows) >= _OBJECT_ERRORS_ROW_CAP:
        summary += (
            f" (truncated at {_OBJECT_ERRORS_ROW_CAP} rows — narrow by "
            "object_name, object_type or owner to see the rest)"
        )
    return summary


InitParametersView = Literal["non_default", "diffs"]

_INIT_PARAMETER_QUERIES: dict[InitParametersView, str] = {
    "non_default": (
        "SELECT inst_id, name, value, ismodified "
        "FROM GV$PARAMETER "
        "WHERE isdefault = 'FALSE' "
        "ORDER BY inst_id, name"
    ),
    "diffs": (
        "SELECT name, COUNT(DISTINCT value) AS distinct_values, "
        "LISTAGG(inst_id || '=' || value, '; ') WITHIN GROUP (ORDER BY inst_id) AS values_by_instance "
        "FROM GV$PARAMETER "
        "GROUP BY name "
        "HAVING COUNT(DISTINCT value) > 1 "
        "ORDER BY name"
    ),
}


def get_init_parameters_query(view: InitParametersView) -> str:
    """Columns verified against a live instance (2026-09-02). diffs is
    RAC-only meaningful (a parameter whose value differs across
    instances) — harmless, just empty, on single-instance.

    Pure function, unit-tested directly — see
    test_instance_health_tier1_query.py.
    """
    return _INIT_PARAMETER_QUERIES[view]


def build_open_cursors_query(sid: int | None) -> tuple[str, dict[str, Any]]:
    """No sid: aggregate open-cursor count per session, worst offenders
    first — the classic ORA-01000 (maximum open cursors exceeded) root
    cause. With sid: every open cursor for that one session, including
    the SQL text, to see exactly what's leaking. Columns verified
    against a live instance (2026-09-02).

    Pure function, unit-tested directly — see
    test_instance_health_tier1_query.py.
    """
    binds: dict[str, Any] = {}
    if sid is not None:
        sql = (
            "SELECT inst_id, sid, user_name, sql_id, sql_text, last_sql_active_time "
            "FROM GV$OPEN_CURSOR "
            "WHERE sid = :sid "
            "ORDER BY inst_id, last_sql_active_time DESC"
        )
        binds["sid"] = sid
    else:
        sql = (
            "SELECT inst_id, sid, user_name, COUNT(*) AS open_cursor_count "
            "FROM GV$OPEN_CURSOR "
            "GROUP BY inst_id, sid, user_name "
            "ORDER BY open_cursor_count DESC "
            "FETCH FIRST 20 ROWS ONLY"
        )
    return sql, binds


LicenseAndOptionsView = Literal["options", "license"]

_LICENSE_AND_OPTIONS_QUERIES: dict[LicenseAndOptionsView, str] = {
    "options": (
        "SELECT inst_id, parameter, value "
        "FROM GV$OPTION "
        "ORDER BY inst_id, parameter"
    ),
    "license": (
        "SELECT inst_id, sessions_max, sessions_warning, sessions_current, "
        "sessions_highwater, users_max "
        "FROM GV$LICENSE "
        "ORDER BY inst_id"
    ),
}


def get_license_and_options_query(view: LicenseAndOptionsView) -> str:
    """options (GV$OPTION): which Oracle Database options this binary
    has compiled in, TRUE/FALSE (Partitioning, Advanced Compression,
    etc.) — a real licensing-exposure check, not just a curiosity, and
    resonant given EBSMCP's own licensable-product angle. license
    (GV$LICENSE): session/user limit configuration — MEDIUM confidence
    on this view specifically, since Oracle stopped really enforcing
    named-user licensing through it years ago and the fields are often
    just zero/unlimited placeholders; the view's existence and columns
    are verified, its practical usefulness varies by instance. Columns
    verified against a live instance (2026-09-02).

    Pure function, unit-tested directly — see
    test_instance_health_tier1_query.py.
    """
    return _LICENSE_AND_OPTIONS_QUERIES[view]


DbSessionStatus = Literal["active", "inactive", "all"]

_DB_SESSION_STATUS_QUERIES: dict[DbSessionStatus, str] = {
    "active": (
        "SELECT inst_id, sid, serial#, username, status, machine, program, module, "
        "logon_time, last_call_et "
        "FROM GV$SESSION "
        "WHERE status = 'ACTIVE' AND type = 'USER' "
        "ORDER BY last_call_et DESC "
        "FETCH FIRST 50 ROWS ONLY"
    ),
    "inactive": (
        "SELECT inst_id, sid, serial#, username, status, machine, program, module, "
        "logon_time, last_call_et "
        "FROM GV$SESSION "
        "WHERE status = 'INACTIVE' AND type = 'USER' "
        "ORDER BY last_call_et DESC "
        "FETCH FIRST 50 ROWS ONLY"
    ),
    "all": (
        "SELECT inst_id, sid, serial#, username, status, machine, program, module, "
        "logon_time, last_call_et "
        "FROM GV$SESSION "
        "WHERE type = 'USER' "
        "ORDER BY status, last_call_et DESC "
        "FETCH FIRST 50 ROWS ONLY"
    ),
}


def get_db_session_status_query(status: DbSessionStatus) -> str:
    """This is Oracle's own session activity concept (GV$SESSION.STATUS
    — ACTIVE means currently executing a call, INACTIVE means connected
    and idle), not an EBS application concept — do not confuse this
    with login_sessions (security_configuration.py, FND_LOGINS-based:
    whether an EBS application session has been logged out yet) or
    named_user_license_count's active/inactive (whether an FND_USER
    *account* is currently enabled). All three answer a genuinely
    different question. BACKGROUND sessions (PMON, SMON, etc.) are
    filtered out — type = 'USER' only — since they're not what "session"
    means in this context. Columns verified against a live instance
    (2026-09-02): confirmed accessible via the public GV$SESSION synonym
    even on the account still waiting on a SELECT_CATALOG_ROLE grant for
    the -qualified form this tool actually ships (12 active / 67
    inactive / 82 background user+system sessions observed).

    Pure function, unit-tested directly — see
    test_instance_health_tier1_query.py.
    """
    return _DB_SESSION_STATUS_QUERIES[status]


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def instance_status(instance: str | None = None) -> dict:
        """Report status, host, version, and startup time for every EBS
        database instance (all nodes, if RAC). Requires an ebs_dba identity
        mapping — takes no parameters, since DBA access is all-or-nothing
        and not Org-ID-scoped.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx, tool_name="instance_status", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT instance_name, status, database_status, host_name, version, startup_time "
                "FROM GV$INSTANCE"
            )

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "instances": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def tablespace_health(view: TablespaceView = "usage", instance: str | None = None) -> dict:
        """Report tablespace allocated/used/free/%used (usage),
        datafiles near their autoextend ceiling (datafile_headroom — the
        classic ORA-01653 trap even when a tablespace looks fine in
        aggregate), or any tablespace unexpectedly not ONLINE (status).
        Defaults to usage. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="tablespace_health", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_tablespace_query(view))
            response = {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }
            if view == "usage":
                response["results"], response["summary"] = annotate_tablespace_usage(rows)
            return response

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def temp_usage(instance: str | None = None) -> dict:
        """Report aggregate TEMP tablespace usage per instance — the
        root cause behind ORA-01652 in concurrent requests when TEMP
        fills up. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="temp_usage", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT tablespace_name, inst_id, "
                "ROUND(SUM(bytes_used) / 1024 / 1024, 1) AS used_mb, "
                "ROUND(SUM(bytes_free) / 1024 / 1024, 1) AS free_mb "
                "FROM GV$TEMP_SPACE_HEADER "
                "GROUP BY tablespace_name, inst_id "
                "ORDER BY tablespace_name, inst_id"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "temp_tablespaces": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def temp_usage_by_session(instance: str | None = None) -> dict:
        """Report which session/SQL_ID is consuming TEMP right now — a
        drill-down from temp_usage. The sql_id feeds a future
        sql_plan_detail lookup. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="temp_usage_by_session", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT s.sid, s.serial#, s.username, tu.tablespace, tu.segtype, tu.sql_id, "
                "ROUND(tu.blocks * 8192 / 1024 / 1024, 1) AS approx_mb "
                "FROM GV$TEMPSEG_USAGE tu "
                "JOIN GV$SESSION s ON s.saddr = tu.session_addr AND s.inst_id = tu.inst_id "
                "ORDER BY approx_mb DESC"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "temp_consumers": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def undo_usage(instance: str | None = None) -> dict:
        """Report undo statistics over the last 24 hours per instance —
        long-running batch job pressure, the root cause behind
        ORA-30036. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="undo_usage", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT inst_id, begin_time, end_time, undoblks, txncount, maxquerylen, "
                "ssolderrcnt, nospaceerrcnt "
                "FROM GV$UNDOSTAT "
                "WHERE begin_time >= SYSDATE - 1 "
                "ORDER BY begin_time DESC"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "undo_stats": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def fra_usage(instance: str | None = None) -> dict:
        """Report Fast Recovery Area headroom per instance — a full FRA
        can halt archiving for the whole instance. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="fra_usage", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT inst_id, name, space_limit, space_used, space_reclaimable, number_of_files, "
                "ROUND(space_used / NULLIF(space_limit, 0) * 100, 1) AS pct_used "
                "FROM GV$RECOVERY_FILE_DEST "
                "ORDER BY pct_used DESC"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "fra_destinations": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def top_segments_in_tablespace(tablespace_name: str, instance: str | None = None) -> dict:
        """List the 20 largest segments in a given tablespace — the
        drill-down for what's actually eating the space once
        tablespace_health flags it (unpurged interface tables, workflow
        history, FND_LOBS attachments). Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="top_segments_in_tablespace",
            target_system="ebs_dba",
            params={"tablespace_name": tablespace_name},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT owner, segment_name, segment_type, ROUND(bytes / 1024 / 1024, 1) AS size_mb "
                "FROM DBA_SEGMENTS "
                "WHERE tablespace_name = :tablespace_name "
                "ORDER BY bytes DESC "
                "FETCH FIRST 20 ROWS ONLY",
                {"tablespace_name": tablespace_name.upper()},
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "tablespace_name": tablespace_name,
                "top_segments": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def database_objects(
        object_name: str | None = None,
        object_type: str | None = None,
        status: str | None = "INVALID",
        instance: str | None = None,
    ) -> dict:
        """Look up database objects by name/type/status — owner, type,
        status, and the created / last_ddl_time / timestamp columns. Use
        this to answer when an object was last compiled or last changed:
        last_ddl_time is Oracle's last-DDL time, so it also moves on
        GRANT, rename and dependency-driven recompiles — an approximation
        of "last compiled", not a compile audit trail. Defaults to every
        INVALID object (the standard post-patch check); pass status=None
        to look one up regardless of status, or status="VALID" to confirm
        a specific object recompiled successfully after a fix. To find out
        WHY something is INVALID, call object_errors — this tool reports
        status, not the compilation error behind it. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="database_objects",
            target_system="ebs_dba",
            params={"object_name": object_name, "object_type": object_type, "status": status},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_database_objects_query(object_name, object_type, status)
            rows = connector.run(sql, binds)
            if rows:
                summary = f"{len(rows)} object(s) found"
            elif status:
                # The status default is INVALID, so a plain lookup by name
                # for a healthy object returns nothing — say why, rather
                # than letting it read as "object does not exist".
                summary = (
                    f"No objects found with status={status.upper()} "
                    "— pass status=None to search every status regardless"
                )
            else:
                summary = "No matching objects found"
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "filters": {"object_name": object_name, "object_type": object_type, "status": status},
                "summary": summary,
                "objects": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def object_errors(
        object_name: str | None = None,
        object_type: str | None = None,
        owner: str | None = None,
        attribute: str | None = "ERROR",
        instance: str | None = None,
    ) -> dict:
        """Report the current compilation errors on stored objects —
        owner, name, type, line, position and the ORA/PLS error text.
        This is what SHOW ERRORS reads, and it's the companion to
        database_objects: that tool says an object is INVALID, this one
        says why. An INVALID object with no rows here was only
        invalidated by a dependency change and will recompile; one with
        rows here is genuinely broken and needs a human. Defaults to real
        errors only — pass attribute=None to include PLSQL_WARNINGS
        warnings, which can sit on objects that compiled fine. Filter by
        owner (e.g. APPS) or object_name on a large instance. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="object_errors",
            target_system="ebs_dba",
            params={
                "object_name": object_name,
                "object_type": object_type,
                "owner": owner,
                "attribute": attribute,
            },
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_object_errors_query(object_name, object_type, owner, attribute)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "filters": {
                    "object_name": object_name,
                    "object_type": object_type,
                    "owner": owner,
                    "attribute": attribute,
                },
                "summary": summarize_object_errors(rows, attribute),
                "errors": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def blocking_locks(instance: str | None = None) -> dict:
        """Report every blocked session and who's blocking it, right
        now. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="blocking_locks", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT blocker.sid AS blocking_sid, blocker.serial# AS blocking_serial, "
                "blocker.username AS blocking_user, "
                "waiter.sid AS waiting_sid, waiter.serial# AS waiting_serial, "
                "waiter.username AS waiting_user, waiter.event, waiter.seconds_in_wait "
                "FROM GV$SESSION waiter "
                "JOIN GV$SESSION blocker "
                "  ON blocker.sid = waiter.blocking_session AND blocker.inst_id = waiter.blocking_instance "
                "WHERE waiter.blocking_session IS NOT NULL "
                "ORDER BY waiter.seconds_in_wait DESC"
            )
            annotated, summary = annotate_blocking_locks(rows)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "summary": summary,
                "blocking_chains": annotated,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def top_sql_by_load(instance: str | None = None) -> dict:
        """List the 20 SQL statements with the highest cumulative
        elapsed time currently in the shared pool — top resource
        consumers. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="top_sql_by_load", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT sql_id, inst_id, executions, elapsed_time, cpu_time, buffer_gets, disk_reads, "
                "SUBSTR(sql_text, 1, 200) AS sql_text_preview "
                "FROM GV$SQL "
                "WHERE executions > 0 "
                "ORDER BY elapsed_time DESC "
                "FETCH FIRST 20 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "top_sql": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def stale_statistics(instance: str | None = None) -> dict:
        """List up to 50 tables overdue for a statistics refresh —
        stale_stats flagged or unanalyzed in the last 30 days. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="stale_statistics", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT owner, table_name, last_analyzed, stale_stats "
                "FROM DBA_TAB_STATISTICS "
                "WHERE stale_stats = 'YES' OR last_analyzed < SYSDATE - 30 OR last_analyzed IS NULL "
                "ORDER BY last_analyzed NULLS FIRST "
                "FETCH FIRST 50 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "stale_tables": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def system_wait_profile(instance: str | None = None) -> dict:
        """Report the instance-wide wait event profile (excluding Idle)
        — the aggregate complement to diagnose_request's per-session
        view. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="system_wait_profile", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT inst_id, event, total_waits, time_waited, average_wait "
                "FROM GV$SYSTEM_EVENT "
                "WHERE wait_class != 'Idle' "
                "ORDER BY time_waited DESC "
                "FETCH FIRST 20 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "wait_profile": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def resource_limits(instance: str | None = None) -> dict:
        """Report how close sessions/processes are to their
        init-parameter ceiling, ahead of an ORA-00020. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="resource_limits", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT inst_id, resource_name, current_utilization, max_utilization, limit_value "
                "FROM GV$RESOURCE_LIMIT "
                "WHERE resource_name IN ('processes', 'sessions') "
                "ORDER BY inst_id, resource_name"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "resource_limits": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def alert_log_errors(instance: str | None = None) -> dict:
        """List up to 50 ORA- errors from the alert log in the last 24
        hours — free (ADR-based), one of the highest-value checks in
        this category. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="alert_log_errors", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT inst_id, originating_timestamp, message_text "
                "FROM GV$DIAG_ALERT_EXT "
                "WHERE message_text LIKE '%ORA-%' AND originating_timestamp >= SYSDATE - 1 "
                "ORDER BY originating_timestamp DESC "
                "FETCH FIRST 50 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "alert_log_entries": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def init_parameters(view: InitParametersView = "non_default", instance: str | None = None) -> dict:
        """Report every init parameter that's been changed from its
        Oracle default (non_default — "what got changed"), or every
        parameter whose value actually differs across RAC instances
        (diffs — node config drift). Defaults to non_default. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="init_parameters", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_init_parameters_query(view))
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
    def undo_segment_detail(instance: str | None = None) -> dict:
        """Report every undo segment's status and current size —
        the per-segment drill-down for undo_usage's aggregate stats. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="undo_segment_detail", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT rs.segment_name, rs.owner, rs.tablespace_name, "
                "rs.status AS segment_status, rst.inst_id, rst.extents, rst.rssize, "
                "rst.status AS runtime_status, rst.curext "
                "FROM DBA_ROLLBACK_SEGS rs "
                "JOIN GV$ROLLSTAT rst ON rst.usn = rs.segment_id "
                "ORDER BY rs.owner, rs.segment_name, rst.inst_id"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "undo_segments": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def open_cursors(sid: int | None = None, instance: str | None = None) -> dict:
        """Report open-cursor count per session, worst offenders first
        (no sid — the ORA-01000 root-cause check), or every open cursor
        for one specific session including SQL text (sid — the
        drill-down). Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="open_cursors", target_system="ebs_dba", params={"sid": sid},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_open_cursors_query(sid)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "sid": sid,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def object_lock_inventory(instance: str | None = None) -> dict:
        """Report every DML/DDL lock currently held on a database
        object, who holds it, and from which session — broader than
        blocking_locks (which only shows session wait chains); this
        also surfaces non-blocking locks that are still worth knowing
        about before a DDL change. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="object_lock_inventory", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT lo.inst_id, lo.session_id, s.serial# AS session_serial, "
                "lo.oracle_username, lo.os_user_name, lo.process, lo.locked_mode, "
                "o.owner, o.object_name, o.object_type "
                "FROM GV$LOCKED_OBJECT lo "
                "JOIN DBA_OBJECTS o ON o.object_id = lo.object_id "
                "JOIN GV$SESSION s ON s.sid = lo.session_id AND s.inst_id = lo.inst_id "
                "ORDER BY lo.inst_id, lo.session_id"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "locked_objects": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def recyclebin(instance: str | None = None) -> dict:
        """List the 50 most recently dropped objects still sitting in
        the recycle bin — a real space-reclaim opportunity (PURGE
        RECYCLEBIN), especially after a cleanup pass leaves large
        dropped tables/indexes behind. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="recyclebin", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT owner, object_name, original_name, operation, type, ts_name, "
                "createtime, droptime, space "
                "FROM DBA_RECYCLEBIN "
                "ORDER BY droptime DESC "
                "FETCH FIRST 50 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "recyclebin_objects": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def dangling_synonyms(instance: str | None = None) -> dict:
        """List up to 50 local synonyms (excludes DB-link-based ones,
        which can't be verified locally) whose target table/view no
        longer exists — post-cleanup hygiene, and a real source of
        confusing ORA-00980/ORA-04043 errors for whoever hits one next. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="dangling_synonyms", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT s.owner, s.synonym_name, s.table_owner, s.table_name, s.db_link "
                "FROM DBA_SYNONYMS s "
                "WHERE s.db_link IS NULL "
                "  AND NOT EXISTS ( "
                "    SELECT 1 FROM DBA_OBJECTS o "
                "    WHERE o.owner = s.table_owner AND o.object_name = s.table_name "
                "  ) "
                "ORDER BY s.owner, s.synonym_name "
                "FETCH FIRST 50 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "dangling_synonyms": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def license_and_options(view: LicenseAndOptionsView = "options", instance: str | None = None) -> dict:
        """Report which Oracle Database options are compiled into this
        binary (options — Partitioning, Advanced Compression, etc.; a
        real licensing-exposure check), or session/user license limit
        configuration (license — MEDIUM confidence on practical
        usefulness, see tool source). Defaults to options. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="license_and_options", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_license_and_options_query(view))
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
    def db_session_status(status: DbSessionStatus = "active", instance: str | None = None) -> dict:
        """Report Oracle database sessions by their own activity status
        — ACTIVE (currently executing a call) or INACTIVE (connected,
        idle), or both (all). This is a database-level concept
        (GV$SESSION.STATUS), not an EBS application concept — for
        whether an EBS application login has ended, use login_sessions
        instead; for whether an FND_USER account is enabled, use
        named_user_license_count. Background sessions (PMON, SMON, etc.)
        are excluded. Capped at 50 rows, busiest/most-recently-active
        first. Defaults to active. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="db_session_status", target_system="ebs_dba", params={"status": status},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_db_session_status_query(status))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "status": status,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def list_ebs_instances() -> dict:
        """List the EBS instances (e.g. PROD, UAT, QA, DEV) configured in
        this deployment that you're entitled to reach, and which one — if
        any — is auto-selected when instance is left unspecified on another
        DBA tool. Call this first if you're not sure what's available, or
        after a call fails asking you to pick one."""
        with resolve_identity_only(
            ctx, tool_name="list_ebs_instances", target_system="ebs_dba", params={}
        ) as identity:
            instances = sorted(permitted_instances(ctx, identity))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "instances": instances,
                "auto_selected": instances[0] if len(instances) == 1 else None,
            }


INSTANCE_HEALTH_TOOLSET = ToolSet(name="instance_health", register=_register)
