"""Patch & version tracking: 4 tools. edition_inventory is new and
genuinely solid ground (DBA_EDITIONS is a core Oracle 11.2+ Edition-Based
Redefinition dictionary view, not EBS-specific at all). patch_history
extends the original recent_applied_patches tool.

adop_session_status (APPLSYS.AD_ADOP_SESSIONS) was added later, once a
live instance was available to verify its column structure and STATUS
code semantics directly rather than guessing — see the tool's own
docstring. This covers session-level status/phase tracking only.
current_patch_edition, adzd_error_log (AD_ZD_LOGS), and the composed
adop_cycle_health remain deliberately NOT covered — same reasoning as
before, just narrower now that one piece of this area has real footing.

db_component_registry (DBA_REGISTRY) was added from the
oracle-base.com/dba/scripts Monitoring inventory — the database-layer
patch registry, distinct in scope from patch_history's EBS-application-
layer coverage. HIGH confidence, standard core Oracle view, columns
verified against a live instance (2026-09-02).

applied_patches (patch_history) was written from memory against an AD.
qualifier and the doubt flagged here was justified: verified live
(2026-09-10) there is no AD schema on this instance, so AD.AD_APPLIED_PATCHES
and AD.AD_BUGS both raise ORA-00942 and the view could never return a row.
AD_APPLIED_PATCHES also has neither BUG_ID nor STATUS, so the join to
AD_BUGS could not have worked either. Both now read through APPS, which is
the access path Oracle documents for application code.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

PatchHistoryView = Literal["applied_patches", "product_versions"]

# PRODUCT_VERSION verified against a live instance (2026-09-02) — the
# original guess (VERSION) doesn't exist on FND_PRODUCT_INSTALLATIONS
# (ORA-00904).
_PRODUCT_VERSIONS_QUERY = (
    "SELECT fpi.application_id, fa.application_short_name, fpi.status, "
    "fpi.patch_level, fpi.product_version "
    "FROM APPS.FND_PRODUCT_INSTALLATIONS fpi "
    "JOIN APPS.FND_APPLICATION fa ON fa.application_id = fpi.application_id "
    "ORDER BY fa.application_short_name"
)

# Without a window this is a "most recent" list; with one it is "everything
# in the window", which on a busy instance is many more rows than 25.
_RECENT_PATCH_LIMIT = 25
_WINDOWED_PATCH_LIMIT = 200


def build_patch_history_query(
    view: PatchHistoryView, days: int | None = None
) -> tuple[str, dict[str, Any]]:
    """SQL + binds for one patch_history view.

    days is a ROLLING window (SYSDATE - N), not a date floor, because the
    questions people actually ask are "in the last 30 days" and "in the last
    24 hours". A YYYY-MM-DD floor like concurrent_requests' `since` cannot
    express the second one at all: the floor for today is midnight today,
    which is not the last 24 hours. days=1 is exactly 24 hours back.

    No column here is interpolated — the window is bound, so a caller cannot
    reach the SQL text.

    Pure function, unit-tested directly — see
    test_patch_version_tracking_query.py.
    """
    if view == "product_versions":
        return _PRODUCT_VERSIONS_QUERY, {}

    binds: dict[str, Any] = {}
    where = ""
    limit = _RECENT_PATCH_LIMIT
    if days is not None:
        where = "WHERE aap.creation_date >= SYSDATE - :days "
        binds["days"] = days
        limit = _WINDOWED_PATCH_LIMIT

    # PATCH_NAME is the patch number a DBA asks about ("36839803"); columns
    # verified live (2026-09-10). No join to AD_BUGS: it would fan each patch
    # out across every bug it delivers, the same defect fixed in
    # concurrent_requests.
    sql = (
        "SELECT aap.applied_patch_id, aap.patch_name AS patch_number, aap.patch_type, "
        "aap.maint_pack_level, aap.creation_date "
        "FROM APPS.AD_APPLIED_PATCHES aap "
        f"{where}"
        "ORDER BY aap.creation_date DESC "
        f"FETCH FIRST {limit} ROWS ONLY"
    )
    return sql, binds


_ADOP_PHASE_COLUMNS = (
    "prepare_status",
    "apply_status",
    "finalize_status",
    "cutover_status",
    "cleanup_status",
)


def build_adop_session_query(session_id: int | None) -> tuple[str, dict[str, Any]]:
    """Columns verified against a live instance (2026-09-02): the table
    is APPLSYS.AD_ADOP_SESSIONS, not AD.AD_ADOP_SESSIONS — this instance
    has no AD schema at all (0 tables owned by AD in ALL_TABLES); a
    synonym check confirmed the real owner is APPLSYS. SESSION_INPUT_DATA
    (a CLOB of session XML) is deliberately excluded — internal/verbose,
    not diagnostic.

    Pure function, unit-tested directly — see
    test_patch_version_tracking_query.py.
    """
    binds: dict[str, Any] = {}
    if session_id is not None:
        where = "WHERE adop_session_id = :session_id "
        limit = ""
        binds["session_id"] = session_id
    else:
        where = ""
        limit = "FETCH FIRST 5 ROWS ONLY "

    sql = (
        "SELECT adop_session_id, status, prepare_status, apply_status, finalize_status, "
        "cutover_status, cleanup_status, abort_status, node_type, node_name#1, edition_name, "
        "appltop_id, prepare_start_date, prepare_end_date, apply_start_date, apply_end_date, "
        "finalize_start_date, finalize_end_date, cutover_start_date, cutover_end_date, "
        "cleanup_start_date, cleanup_end_date, abort_start_date, abort_end_date "
        "FROM APPLSYS.AD_ADOP_SESSIONS "
        f"{where}"
        "ORDER BY adop_session_id DESC "
        f"{limit}"
    )
    return sql, binds


def annotate_adop_sessions(rows: list[dict]) -> tuple[list[dict], str]:
    """Adds is_active (status == 'R', the confirmed "running" code) and,
    when active, current_phase (whichever of the 5 phase columns is
    itself 'R') to each row, plus a one-line summary — same
    enrichment pattern as instance_health.py's annotate_* helpers.
    Status/phase code meanings verified via live data plus published EBS
    DBA references (2026-09-02): R = running, C = completed at the
    session level; Y = phase done, N = not done, X = not applicable, R =
    phase running, F = phase failed, P = at least one patch already
    applied, at the phase level.

    Pure function, unit-tested directly — see
    test_patch_version_tracking_query.py.
    """
    annotated: list[dict] = []
    for row in rows:
        is_active = row.get("status") == "R"
        current_phase = None
        if is_active:
            for column in _ADOP_PHASE_COLUMNS:
                if row.get(column) == "R":
                    current_phase = column.removesuffix("_status")
                    break
        annotated.append({**row, "is_active": is_active, "current_phase": current_phase})

    if not annotated:
        return annotated, "No ADOP sessions found"

    active = [r for r in annotated if r["is_active"]]
    latest = annotated[0]
    if active:
        phase = active[0]["current_phase"] or "between phases"
        summary = f"Session #{active[0]['adop_session_id']} is active — currently in {phase}"
    else:
        summary = f"No ADOP session currently active — most recent is #{latest['adop_session_id']} (status: {latest['status']})"
    return annotated, summary


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def patch_history(
        view: PatchHistoryView = "applied_patches",
        days: int | None = None,
        instance: str | None = None,
    ) -> dict:
        """WHICH PATCHES have been applied, and when (applied_patches), or
        the current release/patchset level per product (product_versions).
        Defaults to applied_patches.

        Use days for "in the last N days": days=30 for the last 30 days,
        days=10 for the last 10, days=1 for the last 24 hours. Without days
        it returns the 25 most recent patches regardless of age.

        This is the tool for "what patches were applied recently".
        adop_session_status is NOT — that reports the progress of R12.2
        patching SESSIONS (prepare/apply/cutover), not which patches they
        delivered. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="patch_history", target_system="ebs_dba",
            params={"view": view, "days": days},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_patch_history_query(view, days)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "window_days": days,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def edition_inventory(instance: str | None = None) -> dict:
        """List the full Edition-Based Redefinition hierarchy — a
        growing pile of orphaned, un-actualized editions signals
        cleanup phases aren't completing. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="edition_inventory", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT edition_name, parent_edition_name, usable "
                "FROM DBA_EDITIONS "
                "ORDER BY edition_name"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "editions": rows,
            }


    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def db_component_registry(instance: str | None = None) -> dict:
        """List every registered Oracle database component (Core RDBMS,
        XDB, Java, etc.) with its version and status — the database
        layer's own patch/version registry. Distinct from patch_history,
        which covers the EBS application layer, not the database itself.
        A component showing anything other than VALID needs attention
        before the next database-layer patch. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="db_component_registry", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT comp_id, comp_name, version, status "
                "FROM DBA_REGISTRY "
                "ORDER BY comp_name"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "components": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def adop_session_status(session_id: int | None = None, instance: str | None = None) -> dict:
        """Report ADOP (R12.2 online patching) SESSION progress: the 5
        most recent sessions by default, or one specific session by ID.
        This reports how patching runs progressed, NOT which patches were
        applied — for "what patches were applied in the last N days", use
        patch_history with days=N.
        ADOP enforces a single in-progress session per instance, so the
        most recent session is always the active one if any is — a
        session with status "R" is actively running right now; "C"
        means it completed. Reports which phase (prepare/apply/finalize/
        cutover/cleanup) is currently running, if any. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="adop_session_status",
            target_system="ebs_dba",
            params={"session_id": session_id},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_adop_session_query(session_id)
            rows = connector.run(sql, binds)
            annotated, summary = annotate_adop_sessions(rows)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "summary": summary,
                "sessions": annotated,
            }


PATCH_VERSION_TRACKING_TOOLSET = ToolSet(name="patch_version_tracking", register=_register)
