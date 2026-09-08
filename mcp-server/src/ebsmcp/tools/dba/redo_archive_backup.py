"""Redo/archive/backup health: four catalog-aligned tools, not one tool
with a growing check enum. The original single redo_archive_backup_status
tool lumped three genuinely different concerns (redo, archive, backup)
under one name; the real architecture-doc catalog names four more
focused tools instead, each with its own narrower view parameter. Same
get_query()-style dict-of-fixed-queries shape as before — view selects
among independent, fixed queries (unrelated row shapes), not a shared
WHERE clause.

SQL confidence, same standard used throughout this DBA batch:
- redo_log_status: HIGH both views — GV$LOG/GV$LOGFILE/GV$LOG_HISTORY are
  standard, textbook Oracle views.
- archive_log_status: HIGH both views — GV$ARCHIVED_LOG, including
  blocks/block_size, are standard columns.
- archive_pipeline_status: MEDIUM-HIGH both views — GV$ARCHIVE_DEST_STATUS/
  GV$ARCHIVE_PROCESSES are standard, slightly more specialized views.
- backup_status: MEDIUM-HIGH (jobs, archivelog_coverage) to MEDIUM
  (by_datafile, controlfile) — see per-view notes below.
  retention_compliance is deliberately narrow (LOW): it returns only the
  configured RMAN retention policy from GV$RMAN_CONFIGURATION, not a
  computed compliance check against actual backup age — that comparison
  needs cross-referencing potentially multiple backup pieces, which is
  real composed-tool territory, not something to synthesize blind in one
  query. Compare against backup_status(view="jobs") by hand.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

RedoLogView = Literal["current", "switch_history"]

_REDO_LOG_QUERIES: dict[RedoLogView, str] = {
    "current": (
        "SELECT l.inst_id, l.group#, l.thread#, l.status AS group_status, l.archived, l.first_time, "
        "lf.member, lf.status AS member_status, lf.type "
        "FROM GV$LOG l "
        "JOIN GV$LOGFILE lf ON lf.group# = l.group# AND lf.inst_id = l.inst_id "
        "ORDER BY l.inst_id, l.group#, lf.member"
    ),
    # NEXT_TIME doesn't exist on GV$LOG_HISTORY — verified against a live
    # instance (2026-09-02), ORA-00904. There's no direct "next switch
    # time" column (NEXT_CHANGE# is SCN-based, not time-based); dropped
    # rather than replaced, since consecutive rows' first_time values
    # already show switch intervals.
    "switch_history": (
        "SELECT inst_id, thread#, sequence#, first_time "
        "FROM GV$LOG_HISTORY "
        "WHERE first_time >= SYSDATE - 1 "
        "ORDER BY first_time DESC"
    ),
}


def get_redo_log_query(view: RedoLogView) -> str:
    return _REDO_LOG_QUERIES[view]


ArchiveLogView = Literal["gaps", "generation_rate"]

_ARCHIVE_LOG_QUERIES: dict[ArchiveLogView, str] = {
    "gaps": (
        "SELECT inst_id, thread#, MAX(sequence#) AS last_archived_sequence, "
        "MAX(completion_time) AS last_archived_time "
        "FROM GV$ARCHIVED_LOG "
        "WHERE deleted = 'NO' "
        "GROUP BY inst_id, thread# "
        "ORDER BY inst_id, thread#"
    ),
    "generation_rate": (
        "SELECT inst_id, TRUNC(completion_time, 'HH24') AS archive_hour, COUNT(*) AS logs_archived, "
        "ROUND(SUM(blocks * block_size) / 1024 / 1024, 1) AS volume_mb "
        "FROM GV$ARCHIVED_LOG "
        "WHERE completion_time >= SYSDATE - 1 AND deleted = 'NO' "
        "GROUP BY inst_id, TRUNC(completion_time, 'HH24') "
        "ORDER BY archive_hour"
    ),
}


def get_archive_log_query(view: ArchiveLogView) -> str:
    return _ARCHIVE_LOG_QUERIES[view]


ArchivePipelineView = Literal["destinations", "processes"]

_ARCHIVE_PIPELINE_QUERIES: dict[ArchivePipelineView, str] = {
    # ERROR_CODE doesn't exist on GV$ARCHIVE_DEST_STATUS — verified
    # against a live instance (2026-09-02), ORA-00904. The real column is
    # just ERROR (the error text itself, not a separate numeric code).
    "destinations": (
        "SELECT ads.inst_id, ads.dest_id, ads.dest_name, ads.status, ads.error, ads.gap_status "
        "FROM GV$ARCHIVE_DEST_STATUS ads "
        "ORDER BY ads.inst_id, ads.dest_id"
    ),
    "processes": (
        "SELECT inst_id, process, status, log_sequence "
        "FROM GV$ARCHIVE_PROCESSES "
        "ORDER BY inst_id, process"
    ),
}


def get_archive_pipeline_query(view: ArchivePipelineView) -> str:
    return _ARCHIVE_PIPELINE_QUERIES[view]


BackupView = Literal["jobs", "by_datafile", "archivelog_coverage", "controlfile", "retention_compliance"]

_BACKUP_QUERIES: dict[BackupView, str] = {
    "jobs": (
        "SELECT inst_id, session_key, input_type, status, start_time, end_time, "
        "ROUND(output_bytes / 1024 / 1024 / 1024, 2) AS output_gb "
        "FROM GV$RMAN_BACKUP_JOB_DETAILS "
        "ORDER BY start_time DESC "
        "FETCH FIRST 20 ROWS ONLY"
    ),
    "by_datafile": (
        "SELECT file#, MAX(checkpoint_time) AS last_backup_checkpoint "
        "FROM GV$BACKUP_DATAFILE "
        "WHERE completion_time IS NOT NULL "
        "GROUP BY file# "
        "ORDER BY file#"
    ),
    "archivelog_coverage": (
        "SELECT inst_id, thread#, sequence#, backup_count, completion_time "
        "FROM GV$ARCHIVED_LOG "
        "WHERE deleted = 'NO' AND backup_count = 0 "
        "ORDER BY completion_time ASC"
    ),
    "controlfile": (
        "SELECT inst_id, device_type, controlfile_type, completion_time, status "
        "FROM GV$BACKUP_CONTROLFILE_SUMMARY "
        "ORDER BY completion_time DESC "
        "FETCH FIRST 10 ROWS ONLY"
    ),
    "retention_compliance": (
        "SELECT inst_id, name, value "
        "FROM GV$RMAN_CONFIGURATION "
        "WHERE name = 'RETENTION POLICY'"
    ),
}


def get_backup_query(view: BackupView) -> str:
    return _BACKUP_QUERIES[view]


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def redo_log_status(view: RedoLogView = "current", instance: str | None = None) -> dict:
        """Report current redo log group/member status including member
        health (current), or log switch history over the last day
        (switch_history). Defaults to current. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="redo_log_status", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_redo_log_query(view))
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
    def archive_log_status(view: ArchiveLogView = "gaps", instance: str | None = None) -> dict:
        """Report the last archived sequence/time per thread (gaps), or
        archived volume per hour over the last day (generation_rate) —
        generation_rate is the direct input for projecting how fast the
        FRA fills. Defaults to gaps. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="archive_log_status", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_archive_log_query(view))
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
    def archive_pipeline_status(view: ArchivePipelineView = "destinations", instance: str | None = None) -> dict:
        """Report archive destination status and errors (destinations —
        a failed mandatory destination can eventually hang the primary
        database), or ARCn archiver process status (processes — are
        enough running to keep up with generation). Defaults to
        destinations. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="archive_pipeline_status", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_archive_pipeline_query(view))
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
    def backup_status(view: BackupView = "jobs", instance: str | None = None) -> dict:
        """Report the 20 most recent RMAN backup jobs (jobs), time since
        last backup per datafile (by_datafile), archived logs not yet
        backed up (archivelog_coverage), recent controlfile autobackups
        (controlfile), or the configured RMAN retention policy
        (retention_compliance — configuration only, not a computed
        compliance check; compare against jobs by hand). Defaults to
        jobs.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx, tool_name="backup_status", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_backup_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }


REDO_ARCHIVE_BACKUP_TOOLSET = ToolSet(name="redo_archive_backup", register=_register)
