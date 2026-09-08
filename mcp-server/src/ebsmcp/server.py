"""MCP server entrypoint: wires config, identity, entitlement, audit, and
the connector together, mounts the (currently one) toolset, and runs.

Run locally against a mock EBS connection, stub identity, no auth:
    cd mcp-server && pip install -e ".[dev]"
    python -m ebsmcp.server

Point the MCP inspector or Claude Desktop at this process (stdio transport
by default) to call server_health and see the full pipeline execute.

Real Entra ID auth activates automatically once EBSMCP_ENTRA_TENANT_ID,
EBSMCP_ENTRA_AUDIENCE, and EBSMCP_RESOURCE_SERVER_URL are all set (see
config.Settings.has_real_entra_config) — there's no separate flag to flip.
Until then, the server runs exactly as it does today: dev_identity_subject
stands in for a real caller. Auth only makes sense over streamable-http;
stdio has no notion of a bearer token at all.
"""

from __future__ import annotations

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from ebsmcp.audit import AuditLogger
from ebsmcp.auth import EntraTokenVerifier, HttpJWKSSource
from ebsmcp.config import Settings, load_settings
from ebsmcp.connectors import MockEBSConnector, OracleEBSConnector
from ebsmcp.connectors.base import EBSConnector
from ebsmcp.identity import IdentityResolver, PostgresIdentityResolver, ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import (
    CONCURRENT_PROCESSING_TOOLSET,
    DIAGNOSE_REQUEST_TOOLSET,
    HEALTH_TOOLSET,
    HIGH_AVAILABILITY_DR_TOOLSET,
    INDEX_HEALTH_TOOLSET,
    INSTANCE_HEALTH_TOOLSET,
    MEMORY_TOOLSET,
    NOTIFICATION_DIAGNOSIS_TOOLSET,
    OUTPUT_PRINTING_TOOLSET,
    PATCH_VERSION_TRACKING_TOOLSET,
    REDO_ARCHIVE_BACKUP_TOOLSET,
    SECURITY_CONFIGURATION_TOOLSET,
    WORKFLOW_TOOLSET,
    ToolContext,
    mount_toolsets,
)


def build_connectors(settings: Settings) -> dict[str, EBSConnector]:
    """One EBSConnector per configured EBS instance (settings.resolved_ebs_instances
    — real ebs_instances if set, else the legacy singular EBS_DB_* vars
    synthesized as one instance), keyed by uppercased instance name.
    """
    instances = settings.resolved_ebs_instances
    if instances:
        return {
            name: OracleEBSConnector(dsn=cfg.dsn, user=cfg.user, password=cfg.password)
            for name, cfg in instances.items()
        }

    # No real EBS credentials configured — fall back to the mock so the
    # server is runnable end to end before any real environment exists.
    # Two rows (simulated RAC) so instance_status's "return every row, not
    # just rows[0]" behavior is visibly exercised locally.
    return {settings.environment.upper(): MockEBSConnector(
        canned_responses={
            "gv$instance": [
                {
                    "instance_name": "MOCKDB1",
                    "status": "OPEN",
                    "database_status": "ACTIVE",
                    "host_name": "mockhost1",
                    "version": "19.0.0.0.0",
                    "startup_time": "2026-08-01T03:00:00",
                },
                {
                    "instance_name": "MOCKDB2",
                    "status": "OPEN",
                    "database_status": "ACTIVE",
                    "host_name": "mockhost2",
                    "version": "19.0.0.0.0",
                    "startup_time": "2026-08-01T03:00:00",
                },
            ],
            "allocated_mb": [
                {"tablespace_name": "APPS_TS_TX_DATA", "allocated_mb": 51200.0,
                 "used_mb": 48640.0, "free_mb": 2560.0, "pct_used": 95.0},
            ],
            "pct_of_max": [
                {"file_name": "/u01/oradata/apps_ts_tx_data01.dbf", "tablespace_name": "APPS_TS_TX_DATA",
                 "bytes": 21474836480, "maxbytes": 32212254720, "autoextensible": "YES", "pct_of_max": 66.7},
            ],
            "extent_management": [
                {"tablespace_name": "APPS_TS_SEED", "status": "READ ONLY",
                 "contents": "PERMANENT", "extent_management": "LOCAL"},
            ],
            "gv$temp_space_header": [
                {"tablespace_name": "TEMP", "inst_id": 1, "used_mb": 4096.0, "free_mb": 12288.0},
            ],
            "gv$tempseg_usage": [
                {"sid": 812, "serial#": 4491, "username": "APPS", "tablespace": "TEMP",
                 "segtype": "SORT", "sql_id": "9fkq9zbjw2vqm", "approx_mb": 640.0},
            ],
            "gv$undostat": [
                {"inst_id": 1, "begin_time": "2026-09-01T05:00:00", "end_time": "2026-09-01T06:00:00",
                 "undoblks": 18422, "txncount": 5031, "maxquerylen": 1800, "ssolderrcnt": 0, "nospaceerrcnt": 0},
            ],
            "gv$recovery_file_dest": [
                {"inst_id": 1, "name": "/u01/fra", "space_limit": 107374182400, "space_used": 64424509440,
                 "space_reclaimable": 10737418240, "number_of_files": 842, "pct_used": 60.0},
            ],
            "dba_segments": [
                {"owner": "APPLSYS", "segment_name": "FND_LOBS", "segment_type": "TABLE", "size_mb": 8192.0},
            ],
            "status = :status": [
                {"owner": "APPS", "object_name": "XX_CUSTOM_PKG", "object_type": "PACKAGE BODY",
                 "status": "INVALID", "created": "2025-03-10T00:00:00",
                 "last_ddl_time": "2026-08-30T22:00:00", "timestamp": "2026-08-30T22:00:00"},
            ],
            "dba_errors": [
                {"owner": "APPS", "name": "XX_CUSTOM_PKG", "type": "PACKAGE BODY",
                 "sequence": 1, "line": 412, "position": 5, "attribute": "ERROR",
                 "text": "PLS-00201: identifier 'XX_UTIL_PKG.GET_ORG_ID' must be declared"},
                {"owner": "APPS", "name": "XX_CUSTOM_PKG", "type": "PACKAGE BODY",
                 "sequence": 2, "line": 412, "position": 5, "attribute": "ERROR",
                 "text": "PL/SQL: Statement ignored"},
            ],
            "blocking_session is not null": [
                {"blocking_sid": 44, "blocking_serial": 102, "blocking_user": "APPS",
                 "waiting_sid": 812, "waiting_serial": 4491, "waiting_user": "APPS",
                 "event": "enq: TX - row lock contention", "seconds_in_wait": 214},
            ],
            "sql_text_preview": [
                {"sql_id": "9fkq9zbjw2vqm", "inst_id": 1, "executions": 412, "elapsed_time": 88420000,
                 "cpu_time": 61200000, "buffer_gets": 9820441, "disk_reads": 12044,
                 "sql_text_preview": "SELECT * FROM GL.GL_JE_LINES WHERE ..."},
            ],
            "dba_tab_statistics": [
                {"owner": "APPLSYS", "table_name": "FND_CONCURRENT_REQUESTS",
                 "last_analyzed": "2026-07-15T02:00:00", "stale_stats": "YES"},
            ],
            "gv$system_event": [
                {"inst_id": 1, "event": "db file sequential read", "total_waits": 8820441,
                 "time_waited": 442100, "average_wait": 0.05},
            ],
            "gv$resource_limit": [
                {"inst_id": 1, "resource_name": "processes", "current_utilization": 340,
                 "max_utilization": 512, "limit_value": "600"},
            ],
            "gv$diag_alert_ext": [
                {"inst_id": 1, "originating_timestamp": "2026-09-01T04:12:00",
                 "message_text": "ORA-00060: deadlock detected while waiting for resource"},
            ],
            # One entry per DBA tool, keyed by a distinguishing substring
            # of that tool's own SQL (MockEBSConnector matches on any
            # substring, not just table names — see connectors/ebs_db.py)
            # so every tool is exercisable locally with no real Oracle
            # connection. Keys are chosen so none collide with each other
            # or with "gv$instance" above.
            "phase_code = 'r' order by": [
                {
                    "request_id": 4010293,
                    "program_name": "Create Accounting",
                    "phase_code": "R",
                    "status_code": "R",
                    "actual_start_date": "2026-09-01T02:00:00",
                    "actual_completion_date": None,
                    "elapsed_minutes": 187.4,
                    "requested_by": "SYSADMIN",
                    "sid": 812,
                    "serial#": 4491,
                    "session_status": "ACTIVE",
                    "sql_id": "9fkq9zbjw2vqm",
                    "wait_event": "db file sequential read",
                }
            ],
            "language = 'us' order by fcqt.user_concurrent_queue_name": [
                {
                    "concurrent_queue_id": 1,
                    "manager_name": "Standard Manager",
                    "running_processes": 3,
                    "max_processes": 5,
                    "enabled_flag": "Y",
                    "control_code": None,
                }
            ],
            "target_processes": [
                {"concurrent_queue_id": 1, "manager_name": "Standard Manager",
                 "target_processes": 5, "actual_processes": 3},
            ],
            "oracle_process_id": [
                {"concurrent_queue_id": 1, "manager_name": "Standard Manager",
                 "concurrent_process_id": 501, "process_status_code": "A", "oracle_process_id": 30211},
            ],
            "running_count": [
                {"program_name": "Create Accounting", "request_count": 12,
                 "running_count": 3, "pending_count": 9},
            ],
            "completion_hour": [
                {"completion_hour": "2026-09-01T09:00:00", "completed_count": 42},
            ],
            # diagnose_request's five internal queries, one canned entry each.
            "elapsed_minutes": [
                {
                    "request_id": 4010293,
                    "phase_code": "R",
                    "status_code": "R",
                    "actual_start_date": "2026-09-01T02:00:00",
                    "elapsed_minutes": 45.0,
                    "program_name": "Create Accounting",
                    "program_short_name": "CREATE_ACCOUNTING",
                    "os_process_id": "30105",
                    "concurrent_program_id": 20450,
                    "program_application_id": 101,
                }
            ],
            "gv$session gs": [
                {
                    "sid": 812,
                    "serial#": 4491,
                    "inst_id": 1,
                    "status": "ACTIVE",
                    "event": "db file sequential read",
                    "wait_class": "User I/O",
                    "blocking_session": None,
                    "sql_id": "9fkq9zbjw2vqm",
                    "module": "CREATE_ACCOUNTING",
                }
            ],
            "gv$session_longops": [
                {
                    "sid": 812,
                    "serial#": 4491,
                    "opname": "Table Scan",
                    "target": "GL.GL_JE_LINES",
                    "sofar": 620000,
                    "totalwork": 1000000,
                    "units": "Rows",
                    "pct_complete": 62.0,
                }
            ],
            "gv$sql sq": [
                {"sql_id": "9fkq9zbjw2vqm", "sql_text": "SELECT * FROM GL.GL_JE_LINES WHERE ..."}
            ],
            "avg_minutes": [
                {"avg_minutes": 10.0, "median_minutes": 8.5},
            ],
            "activity_status = 'error'": [
                {
                    "item_type": "POAPPRV",
                    "item_key": "3010master882",
                    "activity_status": "ERROR",
                    "assigned_user": "SYSADMIN",
                    "begin_date": "2026-08-31T14:12:00",
                }
            ],
            "activity_count": [
                {"activity_status": "DEFERRED", "activity_count": 14},
                {"activity_status": "ACTIVE", "activity_count": 6},
            ],
            "component_status": [
                {"component_id": 301, "component_name": "Workflow Notification Mailer",
                 "component_status": "RUNNING", "last_update_date": "2026-09-01T05:00:00"},
            ],
            "wn.notification_id = :notification_id": [
                {"notification_id": 88231, "message_type": "POAPPRV", "message_name": "APPROVE_REQ",
                 "subject": "PO Approval Required", "status": "OPEN", "mail_status": "MAILED",
                 "recipient_role": "SYSADMIN", "begin_date": "2026-08-31T10:00:00",
                 "end_date": None, "responder": None},
            ],
            "notification_count": [
                {"mail_status": "MAILED", "notification_count": 120},
                {"mail_status": "FAILED", "notification_count": 3},
            ],
            "wias.notification_id": [
                {"item_type": "POAPPRV", "item_key": "3010master882",
                 "activity_status": "ACTIVE", "assigned_user": "SYSADMIN"},
            ],
            "output_file_type is null": [
                {
                    "request_id": 4010301,
                    "program_name": "Print Invoices",
                    "status_code": "C",
                    "actual_completion_date": "2026-09-01T01:30:00",
                }
            ],
            "fp.printer_type": [
                {"printer_name": "HP_FIN_LASER1", "printer_type": "HP LaserJet 4"},
            ],
            "user_concurrent_queue_name = 'output post processor'": [
                {"concurrent_queue_id": 5, "manager_name": "Output Post Processor",
                 "running_processes": 1, "max_processes": 1, "enabled_flag": "Y", "control_code": None},
            ],
            "dba_users": [
                {"username": "APPS", "account_status": "OPEN", "profile": "DEFAULT",
                 "default_tablespace": "APPS_TS_TX_DATA", "password_change_date": "2026-01-15T00:00:00"},
            ],
            "dba_role_privs": [
                {"grantee": "APPS", "privilege": "DBA", "privilege_type": "ROLE"},
            ],
            "password_date": [
                {"user_name": "SYSADMIN", "start_date": "2020-01-01T00:00:00", "end_date": None,
                 "password_date": "2026-07-01T00:00:00", "email_address": "sysadmin@example.com",
                 "description": "System Administrator"},
            ],
            "fnd_unsuccessful_logins": [
                {"login_name": "JDOE", "failed_attempts": 4, "last_attempt": "2026-09-01T23:10:00"},
            ],
            "responsibility_application_id": [
                {"user_name": "SYSADMIN", "responsibility_id": 20420, "application_id": 1,
                 "responsibility_name": "System Administrator",
                 "start_date": "2020-01-01T00:00:00", "end_date": None},
            ],
            "fnd_resp_functions": [
                {"responsibility_id": 20420, "application_id": 1, "action_id": 5551, "rule_type": "F"},
            ],
            "fnd_profile_option_values": [
                {"level_id": 10003, "level_value": 20420, "profile_option_value": "204"},
            ],
            "end_time is null": [
                {"user_name": "JDOE", "start_time": "2026-09-02T08:05:00", "pid": "30422"},
            ],
            "end_time is not null": [
                {"user_name": "JDOE", "start_time": "2026-09-02T07:05:00",
                 "end_time": "2026-09-02T07:45:00", "pid": "30401"},
            ],
            "fu.end_date is null or": [
                {"named_user_count": 842},
            ],
            "fu.end_date is not null and": [
                {"named_user_count": 57},
            ],
            "responsibility_a": [
                {"user_name": "JDOE"},
            ],
            "sgainfo": [
                {"inst_id": 1, "source": "SGA", "name": "Shared Pool Size", "value_mb": 512.0},
                {"inst_id": 1, "source": "SGAINFO", "name": "Buffer Cache Size", "value_mb": 2048.0},
            ],
            "gv$sgastat": [
                {"inst_id": 1, "pool": "shared pool", "name": "free memory", "value_mb": 128.0},
            ],
            "over allocation count": [
                {"inst_id": 1, "name": "total PGA allocated", "value": 268435456, "unit": "bytes"},
                {"inst_id": 1, "name": "over allocation count", "value": 0, "unit": "count"},
            ],
            "pga_alloc_mem": [
                {"sid": 812, "serial#": 4491, "username": "APPS",
                 "pga_used_mem": 41943040, "pga_alloc_mem": 52428800, "pga_max_mem": 62914560},
            ],
            "gv$librarycache": [
                {"inst_id": 1, "namespace": "SQL AREA", "gets": 8820441, "gethits": 8750210,
                 "gethit_pct": 99.2, "pins": 12044221, "pinhits": 11980442, "pinhit_pct": 99.5,
                 "reloads": 442, "invalidations": 12},
            ],
            "gv$buffer_pool_statistics": [
                {"inst_id": 1, "pool_name": "DEFAULT", "physical_reads": 442100,
                 "db_block_gets": 8820441, "consistent_gets": 61200441, "buffer_hit_pct": 99.4},
            ],
            "gv$sga_target_advice": [
                {"inst_id": 1, "sga_size": 4096, "sga_size_factor": 1.0,
                 "estd_db_time": 120000, "estd_db_time_factor": 1.0},
            ],
            "gv$db_cache_advice": [
                {"inst_id": 1, "size_for_estimate": 2048, "size_factor": 1.0, "estd_physical_read_factor": 1.0},
            ],
            "gv$shared_pool_advice": [
                {"inst_id": 1, "shared_pool_size_for_estimate": 512, "shared_pool_size_factor": 1.0,
                 "estd_lc_load_time": 0},
            ],
            "gv$sql_workarea_active": [
                {"sid": 812, "inst_id": 1, "sql_id": "9fkq9zbjw2vqm", "operation_type": "HASH-JOIN",
                 "policy": "AUTO", "actual_mem_used": 4194304, "tempseg_size": 20971520},
            ],
            # GV$ARCHIVED_LOG is referenced by three different queries
            # (gaps/generation_rate/archivelog_coverage) — the more
            # specific keys must come before the general "gv$archived_log"
            # one, since MockEBSConnector returns on the first substring
            # match found (see the concurrent_load_trend collision fixed
            # earlier in this project).
            "archive_hour": [
                {"inst_id": 1, "archive_hour": "2026-09-01T05:00:00", "logs_archived": 4, "volume_mb": 512.0},
            ],
            "backup_count = 0": [
                {"inst_id": 1, "thread#": 1, "sequence#": 48214, "backup_count": 0,
                 "completion_time": "2026-09-01T06:10:00"},
            ],
            "gv$archived_log": [
                {
                    "inst_id": 1,
                    "thread#": 1,
                    "last_archived_sequence": 48213,
                    "last_archived_time": "2026-09-01T05:45:00",
                }
            ],
            "gv$logfile": [
                {"inst_id": 1, "group#": 1, "thread#": 1, "group_status": "CURRENT", "archived": "NO",
                 "first_time": "2026-09-01T06:00:00", "member": "/u01/oradata/redo01a.log",
                 "member_status": None, "type": "ONLINE"},
            ],
            "gv$log_history": [
                {"inst_id": 1, "thread#": 1, "sequence#": 48213, "first_time": "2026-09-01T05:40:00"},
            ],
            "gv$archive_dest_status": [
                {"inst_id": 1, "dest_id": 1, "dest_name": "LOG_ARCHIVE_DEST_1",
                 "status": "VALID", "error": None, "gap_status": "NO GAP"},
            ],
            "gv$archive_processes": [
                {"inst_id": 1, "process": "ARC0", "status": "ACTIVE", "log_sequence": 48214},
            ],
            "gv$rman_backup_job_details": [
                {
                    "inst_id": 1,
                    "session_key": 9821,
                    "input_type": "DB FULL",
                    "status": "COMPLETED",
                    "start_time": "2026-09-01T01:00:00",
                    "end_time": "2026-09-01T01:42:00",
                    "output_gb": 84.6,
                }
            ],
            "gv$backup_datafile": [
                {"file#": 1, "last_backup_checkpoint": "2026-09-01T01:00:00"},
            ],
            "gv$backup_controlfile_summary": [
                {"inst_id": 1, "device_type": "DISK", "controlfile_type": "AUTOBACKUP",
                 "completion_time": "2026-09-01T01:45:00", "status": "AVAILABLE"},
            ],
            "gv$rman_configuration": [
                {"inst_id": 1, "name": "RETENTION POLICY", "value": "TO REDUNDANCY 2"},
            ],
            "gv$dataguard_stats": [
                {"inst_id": 1, "name": "transport lag", "value": "+00 00:00:02", "unit": "day(2) to second(0) interval",
                 "time_computed": "2026-09-01T06:00:00"},
                {"inst_id": 1, "name": "apply lag", "value": "+00 00:00:05", "unit": "day(2) to second(0) interval",
                 "time_computed": "2026-09-01T06:00:00"},
            ],
            "gv$restore_point": [
                {"inst_id": 1, "name": "PRE_PATCH_2026_08", "scn": 48213991, "time": "2026-08-30T22:00:00",
                 "guarantee_flashback_database": "YES", "storage_size": 1073741824},
            ],
            "gv$cluster_interconnects": [
                {"inst_id": 1, "interconnect_name": "eth1", "ip_address": "10.0.1.11",
                 "is_public": "NO", "source": "OCR"},
            ],
            "gv$active_services": [
                {"inst_id": 1, "service_id": 3, "name": "ebsprod", "network_name": "ebsprod.example.com",
                 "goal": "THROUGHPUT", "blocked": "NO", "clb_goal": "LONG"},
            ],
            "dba_services": [
                {"service_id": 3, "name": "ebsprod", "network_name": "ebsprod.example.com",
                 "creation_date": "2025-01-10T00:00:00", "goal": "THROUGHPUT", "clb_goal": "LONG"},
            ],
            "ad_applied_patches": [
                {
                    "applied_patch_id": 991234,
                    "patch_number": 33456789,
                    "creation_date": "2026-08-15T00:00:00",
                    "status": "S",
                }
            ],
            "fnd_product_installations": [
                {"application_id": 101, "application_short_name": "SQLGL",
                 "status": "I", "patch_level": "R12.PF.C.6", "version": "12.0.0"},
            ],
            "dba_editions": [
                {"edition_name": "ORA$BASE", "parent_edition_name": None, "usable": "YES"},
                {"edition_name": "EBS_C_20260901", "parent_edition_name": "ORA$BASE", "usable": "YES"},
            ],
            "ad_adop_sessions": [
                {
                    "adop_session_id": 47, "status": "R", "prepare_status": "Y", "apply_status": "R",
                    "finalize_status": "N", "cutover_status": "N", "cleanup_status": "N",
                    "abort_status": "X", "node_type": "master", "node_name#1": "ebsapp01",
                    "edition_name": "V_20260901_0800", "appltop_id": 2045,
                    "prepare_start_date": "2026-09-01T08:00:00", "prepare_end_date": "2026-09-01T08:15:00",
                    "apply_start_date": "2026-09-01T08:20:00", "apply_end_date": None,
                    "finalize_start_date": None, "finalize_end_date": None,
                    "cutover_start_date": None, "cutover_end_date": None,
                    "cleanup_start_date": None, "cleanup_end_date": None,
                    "abort_start_date": None, "abort_end_date": None,
                },
            ],
            "dba_registry": [
                {"comp_id": "XDB", "comp_name": "Oracle XML Database", "version": "23.1.0.0.0", "status": "VALID"},
            ],
            "dba_indexes": [
                {"owner": "APPS", "index_name": "XX_CUSTOM_IDX1", "table_owner": "APPS",
                 "table_name": "XX_CUSTOM_TABLE", "status": "UNUSABLE"},
            ],
            "dba_cons_columns": [
                {"owner": "AP", "table_name": "AP_INVOICES_ALL",
                 "fk_constraint_name": "AP_INVOICES_N_FK1", "column_name": "VENDOR_ID"},
            ],
            "admin_option": [
                {"grantee": "XX_CUSTOM_ROLE", "privilege": "SELECT ANY DICTIONARY", "admin_option": "NO"},
            ],
            "dba_tab_privs": [
                {"grantee": "XX_REPORT_USER", "owner": "APPS", "table_name": "AP_INVOICES_ALL",
                 "privilege": "SELECT", "grantable": "NO"},
            ],
            "dba_db_links": [
                {"owner": "APPS", "db_link": "GL_INTERFACE_LINK.WORLD", "username": "GL_INTF",
                 "host": "erpfin.example.com", "created": "2024-02-10T00:00:00"},
            ],
            "gv$dblink": [
                {"inst_id": 1, "db_link": "GL_INTERFACE_LINK.WORLD", "owner_id": 105,
                 "logged_on": "YES", "heterogeneous": "NO", "open_cursors": 2},
            ],
            "isdefault = 'false'": [
                {"inst_id": 1, "name": "processes", "value": "3000", "ismodified": "FALSE"},
            ],
            "having count(distinct value)": [
                {"name": "sga_target", "distinct_values": 2, "values_by_instance": "1=8G; 2=6G"},
            ],
            "dba_rollback_segs": [
                {"segment_name": "_SYSSMU12_123456789$", "owner": "SYS", "tablespace_name": "UNDOTBS1",
                 "segment_status": "ONLINE", "inst_id": 1, "extents": 24, "rssize": 104857600,
                 "runtime_status": "ONLINE", "curext": 5},
            ],
            "open_cursor_count": [
                {"inst_id": 1, "sid": 812, "user_name": "APPS", "open_cursor_count": 340},
            ],
            "last_sql_active_time": [
                {"inst_id": 1, "sid": 812, "user_name": "APPS", "sql_id": "9fkq9zbjw2vqm",
                 "sql_text": "SELECT * FROM AP_INVOICES_ALL WHERE ...",
                 "last_sql_active_time": "2026-09-02T05:10:00"},
            ],
            "gv$locked_object": [
                {"inst_id": 1, "session_id": 812, "session_serial": 4491, "oracle_username": "APPS",
                 "os_user_name": "applmgr", "process": "12345", "locked_mode": "3",
                 "owner": "AP", "object_name": "AP_INVOICES_ALL", "object_type": "TABLE"},
            ],
            "dba_recyclebin": [
                {"owner": "APPS", "object_name": "BIN$abc123==$0", "original_name": "XX_OLD_STAGING_TBL",
                 "operation": "DROP", "type": "TABLE", "ts_name": "APPS_TS_TX_DATA",
                 "createtime": "2026-01-15T00:00:00", "droptime": "2026-08-20T00:00:00", "space": 4096},
            ],
            "dba_synonyms": [
                {"owner": "APPS", "synonym_name": "XX_OLD_SYNONYM", "table_owner": "APPS",
                 "table_name": "XX_DROPPED_TABLE", "db_link": None},
            ],
            "gv$option": [
                {"inst_id": 1, "parameter": "Partitioning", "value": "TRUE"},
            ],
            "gv$license": [
                {"inst_id": 1, "sessions_max": 0, "sessions_warning": 0, "sessions_current": 842,
                 "sessions_highwater": 1200, "users_max": 0},
            ],
            "status = 'active' and type": [
                {"inst_id": 1, "sid": 812, "serial#": 4491, "username": "APPS", "status": "ACTIVE",
                 "machine": "ebsapp01", "program": "JDBC Thin Client", "module": "AP Invoice Workbench",
                 "logon_time": "2026-09-02T06:28:13", "last_call_et": 2},
            ],
            "status = 'inactive' and type": [
                {"inst_id": 1, "sid": 175, "serial#": 102, "username": "APPS", "status": "INACTIVE",
                 "machine": "ebsapp01", "program": "JDBC Thin Client", "module": "GL Journal Entry",
                 "logon_time": "2026-09-02T05:23:52", "last_call_et": 640},
            ],
            "where type = 'user' order by status": [
                {"inst_id": 1, "sid": 812, "serial#": 4491, "username": "APPS", "status": "ACTIVE",
                 "machine": "ebsapp01", "program": "JDBC Thin Client", "module": "AP Invoice Workbench",
                 "logon_time": "2026-09-02T06:28:13", "last_call_et": 2},
                {"inst_id": 1, "sid": 175, "serial#": 102, "username": "APPS", "status": "INACTIVE",
                 "machine": "ebsapp01", "program": "JDBC Thin Client", "module": "GL Journal Entry",
                 "logon_time": "2026-09-02T05:23:52", "last_call_et": 640},
            ],
        }
    )}


def build_identity_resolver(settings: Settings) -> IdentityResolver:
    if settings.has_real_identity_db:
        return PostgresIdentityResolver(
            db_url=settings.identity_db_url,  # type: ignore[arg-type]
            environment=settings.environment,
        )

    # No real identity-mapping database configured — fall back to fixed
    # stub mappings. Org ID 204 / Vision Operations matches the example row
    # in the architecture doc. Both mappings share dev_identity_subject
    # (one person holding both a functional and a DBA persona mapping, same
    # as identity-service's schema allows for real people) so that a local
    # run can exercise both server_health and instance_status without any
    # extra env var to configure.
    return StubIdentityResolver(
        ResolvedIdentity(
            subject=settings.dev_identity_subject,
            environment=settings.environment,
            target_system="ebs",
            mapped_role="AP_MANAGER",
            allowed_org_ids=("204",),
        ),
        ResolvedIdentity(
            subject=settings.dev_identity_subject,
            environment=settings.environment,
            target_system="ebs_dba",
            mapped_role="Senior DBA — patching access",
            allowed_org_ids=(),
        ),
    )


def build_transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection for streamable-http. Both allowed_hosts and
    allowed_origins default to settings' own empty-list default, which
    matches TransportSecuritySettings' own default — rejecting every
    request until a deployment explicitly names its real hostname(s). Never
    silently widened to "allow everything" here; an empty list is a
    deliberate fail-closed starting point, not a placeholder to guess past.
    """
    return TransportSecuritySettings(
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )


def build_app(settings: Settings) -> MCPServer:
    auth_kwargs: dict = {}
    if settings.transport == "streamable-http" and settings.has_real_entra_config:
        auth_kwargs = {
            "token_verifier": EntraTokenVerifier(
                jwks_source=HttpJWKSSource(settings.entra_jwks_uri),
                issuer=settings.entra_issuer_url,
                audience=settings.entra_audience,  # type: ignore[arg-type]
                subject_claim=settings.entra_subject_claim,
            ),
            "auth": AuthSettings(
                issuer_url=settings.entra_issuer_url,
                resource_server_url=settings.resource_server_url,  # type: ignore[arg-type]
            ),
        }

    app = MCPServer("ebsmcp", **auth_kwargs)

    connectors = build_connectors(settings)
    audit = AuditLogger()
    entitlement = EntitlementFilter()
    identity_resolver = build_identity_resolver(settings)

    ctx = ToolContext(
        connectors=connectors,
        identity_resolver=identity_resolver,
        entitlement=entitlement,
        audit=audit,
        environment=settings.environment,
        dev_subject=settings.dev_identity_subject,
    )

    mount_toolsets(
        app,
        ctx,
        [
            HEALTH_TOOLSET,
            INSTANCE_HEALTH_TOOLSET,
            CONCURRENT_PROCESSING_TOOLSET,
            DIAGNOSE_REQUEST_TOOLSET,
            WORKFLOW_TOOLSET,
            NOTIFICATION_DIAGNOSIS_TOOLSET,
            OUTPUT_PRINTING_TOOLSET,
            SECURITY_CONFIGURATION_TOOLSET,
            MEMORY_TOOLSET,
            REDO_ARCHIVE_BACKUP_TOOLSET,
            HIGH_AVAILABILITY_DR_TOOLSET,
            PATCH_VERSION_TRACKING_TOOLSET,
            INDEX_HEALTH_TOOLSET,
        ],
    )
    return app


def main() -> None:
    settings = load_settings()
    app = build_app(settings)

    if settings.transport == "streamable-http":
        app.run(
            transport="streamable-http",
            host=settings.http_host,
            port=settings.http_port,
            transport_security=build_transport_security(settings),
        )
    else:
        app.run(transport="stdio")


if __name__ == "__main__":
    main()
