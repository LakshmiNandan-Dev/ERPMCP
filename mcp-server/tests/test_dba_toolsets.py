"""One success-case test per remaining DBA tool category, through a real
ClientSession over the in-memory transport (same rationale as
test_example_health_tool.py and test_instance_health_tool.py).

Deliberately does NOT repeat the unmapped-caller/persona-boundary denial
cases for each of these nine — that plumbing (resolve_scoped_call's
ebs_dba branch) is identical for every tool here and is already proven
once, in full, by test_instance_health_tool.py plus the direct unit
tests in test_resolve_scoped_call.py. Re-proving the same shared
resolve_scoped_call behavior nine more times through the full MCP
protocol would be repetition, not additional coverage — what's actually
new per tool here is its own SQL/response shape, which is what each test
below checks.
"""

from __future__ import annotations

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors import MockEBSConnector
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import ToolContext, ToolSet, mount_toolsets
from ebsmcp.tools.dba import (
    CONCURRENT_PROCESSING_TOOLSET,
    HIGH_AVAILABILITY_DR_TOOLSET,
    INDEX_HEALTH_TOOLSET,
    INSTANCE_HEALTH_TOOLSET,
    MEMORY_TOOLSET,
    OUTPUT_PRINTING_TOOLSET,
    PATCH_VERSION_TRACKING_TOOLSET,
    REDO_ARCHIVE_BACKUP_TOOLSET,
    SECURITY_CONFIGURATION_TOOLSET,
    WORKFLOW_TOOLSET,
)

DBA_SUBJECT = "dba@corp.com"

# Matches server.py's build_connectors canned_responses exactly, so these
# tests exercise the same fixtures a local manual run would see.
CANNED_RESPONSES = {
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
        {"concurrent_queue_id": 1, "manager_name": "Standard Manager",
         "running_processes": 3, "max_processes": 5, "enabled_flag": "Y", "control_code": None},
    ],
    "target_processes": [
        {"concurrent_queue_id": 1, "manager_name": "Standard Manager",
         "target_processes": 5, "actual_processes": 3},
    ],
    "running_count": [
        {"program_name": "Create Accounting", "request_count": 12,
         "running_count": 3, "pending_count": 9},
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
    ],
    "fp.printer_type": [
        {"printer_name": "HP_FIN_LASER1", "printer_type": "HP LaserJet 4"},
    ],
    "user_concurrent_queue_name = 'output post processor'": [
        {"concurrent_queue_id": 5, "manager_name": "Output Post Processor",
         "running_processes": 1, "max_processes": 1, "enabled_flag": "Y", "control_code": None},
    ],
    "output_file_type is null": [
        {
            "request_id": 4010301,
            "program_name": "Print Invoices",
            "status_code": "C",
            "actual_completion_date": "2026-09-01T01:30:00",
        }
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
    ],
    "gv$sgastat": [
        {"inst_id": 1, "pool": "shared pool", "name": "free memory", "value_mb": 128.0},
    ],
    "over allocation count": [
        {"inst_id": 1, "name": "total PGA allocated", "value": 268435456, "unit": "bytes"},
    ],
    "pga_alloc_mem": [
        {"sid": 812, "serial#": 4491, "username": "APPS",
         "pga_used_mem": 41943040, "pga_alloc_mem": 52428800, "pga_max_mem": 62914560},
    ],
    "gv$librarycache": [
        {"inst_id": 1, "namespace": "SQL AREA", "gets": 8820441, "gethit_pct": 99.2},
    ],
    "gv$sga_target_advice": [
        {"inst_id": 1, "sga_size": 4096, "sga_size_factor": 1.0,
         "estd_db_time": 120000, "estd_db_time_factor": 1.0},
    ],
    "gv$sql_workarea_active": [
        {"sid": 812, "inst_id": 1, "sql_id": "9fkq9zbjw2vqm", "operation_type": "HASH-JOIN",
         "policy": "AUTO", "actual_mem_used": 4194304, "tempseg_size": 20971520},
    ],
    "gv$logfile": [
        {"inst_id": 1, "group#": 1, "thread#": 1, "group_status": "CURRENT", "archived": "NO",
         "first_time": "2026-09-01T06:00:00", "member": "/u01/oradata/redo01a.log",
         "member_status": None, "type": "ONLINE"},
    ],
    "gv$archived_log": [
        {"inst_id": 1, "thread#": 1, "last_archived_sequence": 48213,
         "last_archived_time": "2026-09-01T05:45:00"},
    ],
    "gv$archive_dest_status": [
        {"inst_id": 1, "dest_id": 1, "dest_name": "LOG_ARCHIVE_DEST_1",
         "status": "VALID", "error": None, "gap_status": "NO GAP"},
    ],
    "gv$rman_backup_job_details": [
        {"inst_id": 1, "session_key": 9821, "input_type": "DB FULL", "status": "COMPLETED",
         "start_time": "2026-09-01T01:00:00", "end_time": "2026-09-01T01:42:00", "output_gb": 84.6},
    ],
    "gv$dataguard_stats": [
        {"inst_id": 1, "name": "transport lag", "value": "+00 00:00:02",
         "unit": "day(2) to second(0) interval", "time_computed": "2026-09-01T06:00:00"},
    ],
    "gv$active_services": [
        {"inst_id": 1, "service_id": 3, "name": "ebsprod", "network_name": "ebsprod.example.com",
         "goal": "THROUGHPUT", "blocked": "NO", "clb_goal": "LONG"},
    ],
    "ad_applied_patches": [
        {"applied_patch_id": 991234, "patch_number": 33456789,
         "creation_date": "2026-08-15T00:00:00", "status": "S"},
    ],
    "fnd_product_installations": [
        {"application_id": 101, "application_short_name": "SQLGL",
         "status": "I", "patch_level": "R12.PF.C.6", "version": "12.0.0"},
    ],
    "dba_editions": [
        {"edition_name": "ORA$BASE", "parent_edition_name": None, "usable": "YES"},
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


def build_test_app(toolset: ToolSet, canned: dict | None = None) -> MCPServer:
    """canned defaults to the shared fixture set; a test overrides it only
    when it needs a query to come back empty, which the substring-keyed
    MockEBSConnector can't express through binds."""
    app = MCPServer("ebsmcp-test")
    ctx = ToolContext(
        connectors={"TEST": MockEBSConnector(
            canned_responses=CANNED_RESPONSES if canned is None else canned
        )},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject=DBA_SUBJECT,
                environment="test",
                target_system="ebs_dba",
                mapped_role="Senior DBA — patching access",
                allowed_org_ids=(),
            )
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject=DBA_SUBJECT,
    )
    mount_toolsets(app, ctx, [toolset])
    return app


async def call_tool(app: MCPServer, tool_name: str, args: dict | None = None):
    transport = InMemoryTransport(app)
    async with transport._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, args or {})


@pytest.mark.asyncio
async def test_concurrent_requests_default_status():
    """Full-protocol proof for the default call (status="running"); every
    other status/filter combination is unit-tested directly against
    build_query in test_concurrent_requests_query.py — no need to
    exercise all five statuses through the MCP wire protocol when the
    thing that actually varies is WHERE-clause/bind construction, not
    resolve_scoped_call's plumbing."""
    result = await call_tool(build_test_app(CONCURRENT_PROCESSING_TOOLSET), "concurrent_requests")
    assert not result.is_error
    text = result.content[0].text
    assert "Create Accounting" in text
    assert '"elapsed_minutes": 187.4' in text
    assert '"sid": 812' in text
    assert '"wait_event": "db file sequential read"' in text
    assert '"summary": "1 running request(s) — avg 187.4 min, min 187.4 min, max 187.4 min"' in text


@pytest.mark.asyncio
async def test_concurrent_requests_filtered_by_program_name():
    result = await call_tool(
        build_test_app(CONCURRENT_PROCESSING_TOOLSET),
        "concurrent_requests",
        {"program_name": "Create Accounting"},
    )
    assert not result.is_error
    assert "Create Accounting" in result.content[0].text


@pytest.mark.asyncio
async def test_manager_status():
    result = await call_tool(build_test_app(CONCURRENT_PROCESSING_TOOLSET), "manager_status")
    assert not result.is_error
    assert "Standard Manager" in result.content[0].text


@pytest.mark.asyncio
async def test_manager_capacity_default_view():
    result = await call_tool(build_test_app(CONCURRENT_PROCESSING_TOOLSET), "manager_capacity")
    assert not result.is_error
    assert '"target_processes": 5' in result.content[0].text


@pytest.mark.asyncio
async def test_concurrent_load_trend_default_group_by():
    result = await call_tool(build_test_app(CONCURRENT_PROCESSING_TOOLSET), "concurrent_load_trend")
    assert not result.is_error
    assert '"running_count": 3' in result.content[0].text


@pytest.mark.asyncio
async def test_workflow_activities_default_status():
    """Full-protocol proof for the default call (status="error"); other
    status/filter combinations are unit-tested directly against
    build_query in test_workflow_activities_query.py."""
    result = await call_tool(build_test_app(WORKFLOW_TOOLSET), "workflow_activities")
    assert not result.is_error
    assert "POAPPRV" in result.content[0].text


@pytest.mark.asyncio
async def test_workflow_backlog():
    result = await call_tool(build_test_app(WORKFLOW_TOOLSET), "workflow_backlog")
    assert not result.is_error
    assert '"activity_count": 14' in result.content[0].text


@pytest.mark.asyncio
async def test_mailer_component_status():
    result = await call_tool(build_test_app(WORKFLOW_TOOLSET), "mailer_component_status")
    assert not result.is_error
    assert "Workflow Notification Mailer" in result.content[0].text


@pytest.mark.asyncio
async def test_notifications_default_aggregate():
    """Full-protocol proof for the default call (no notification_id ->
    aggregate); detail-vs-summary branching and bind discipline are
    unit-tested directly in test_notifications_query.py."""
    result = await call_tool(build_test_app(WORKFLOW_TOOLSET), "notifications")
    assert not result.is_error
    assert '"notification_count": 120' in result.content[0].text


@pytest.mark.asyncio
async def test_printer_registration_status():
    result = await call_tool(build_test_app(OUTPUT_PRINTING_TOOLSET), "printer_registration_status")
    assert not result.is_error
    assert "HP_FIN_LASER1" in result.content[0].text


@pytest.mark.asyncio
async def test_opp_status():
    result = await call_tool(build_test_app(OUTPUT_PRINTING_TOOLSET), "opp_status")
    assert not result.is_error
    assert "Output Post Processor" in result.content[0].text


@pytest.mark.asyncio
async def test_output_file_errors():
    result = await call_tool(build_test_app(OUTPUT_PRINTING_TOOLSET), "output_file_errors")
    assert not result.is_error
    assert "Print Invoices" in result.content[0].text


@pytest.mark.asyncio
async def test_db_level_accounts_default_view():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "db_level_accounts")
    assert not result.is_error
    assert '"account_status": "OPEN"' in result.content[0].text


@pytest.mark.asyncio
async def test_fnd_user_status():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "fnd_user_status",
                              {"username": "SYSADMIN"})
    assert not result.is_error
    assert "System Administrator" in result.content[0].text


@pytest.mark.asyncio
async def test_failed_login_attempts():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "failed_login_attempts")
    assert not result.is_error
    assert '"failed_attempts": 4' in result.content[0].text


@pytest.mark.asyncio
async def test_responsibility_assignments():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "responsibility_assignments")
    assert not result.is_error
    assert "System Administrator" in result.content[0].text


@pytest.mark.asyncio
async def test_responsibility_privileges():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "responsibility_privileges",
                              {"responsibility_id": 20420})
    assert not result.is_error
    assert '"action_id": 5551' in result.content[0].text


@pytest.mark.asyncio
async def test_profile_values():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "profile_values",
                              {"profile_option_name": "MO_SECURITY_PROFILE_ID"})
    assert not result.is_error
    assert '"profile_option_value": "204"' in result.content[0].text


@pytest.mark.asyncio
async def test_login_sessions_default_active():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "login_sessions")
    assert not result.is_error
    assert '"pid": "30422"' in result.content[0].text


@pytest.mark.asyncio
async def test_login_sessions_closed():
    result = await call_tool(
        build_test_app(SECURITY_CONFIGURATION_TOOLSET), "login_sessions", {"view": "closed"}
    )
    assert not result.is_error
    assert '"pid": "30401"' in result.content[0].text


@pytest.mark.asyncio
async def test_named_user_license_count():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "named_user_license_count")
    assert not result.is_error
    assert '"named_user_count": 842' in result.content[0].text


@pytest.mark.asyncio
async def test_named_user_license_count_inactive():
    result = await call_tool(
        build_test_app(SECURITY_CONFIGURATION_TOOLSET), "named_user_license_count", {"status": "inactive"}
    )
    assert not result.is_error
    assert '"named_user_count": 57' in result.content[0].text


@pytest.mark.asyncio
async def test_sod_conflict_scan():
    result = await call_tool(
        build_test_app(SECURITY_CONFIGURATION_TOOLSET),
        "sod_conflict_scan",
        {"responsibility_a": "Payables Manager", "responsibility_b": "Payables Payment"},
    )
    assert not result.is_error
    assert "JDOE" in result.content[0].text


@pytest.mark.asyncio
async def test_sga_status_default_view():
    result = await call_tool(build_test_app(MEMORY_TOOLSET), "sga_status")
    assert not result.is_error
    assert "Shared Pool Size" in result.content[0].text


@pytest.mark.asyncio
async def test_pga_status_default_view():
    result = await call_tool(build_test_app(MEMORY_TOOLSET), "pga_status")
    assert not result.is_error
    assert '"value": 268435456' in result.content[0].text


@pytest.mark.asyncio
async def test_cache_efficiency_default_cache():
    result = await call_tool(build_test_app(MEMORY_TOOLSET), "cache_efficiency")
    assert not result.is_error
    assert '"gethit_pct": 99.2' in result.content[0].text


@pytest.mark.asyncio
async def test_memory_advisor():
    result = await call_tool(build_test_app(MEMORY_TOOLSET), "memory_advisor")
    assert not result.is_error
    assert '"sga_size": 4096' in result.content[0].text


@pytest.mark.asyncio
async def test_workarea_profile():
    result = await call_tool(build_test_app(MEMORY_TOOLSET), "workarea_profile")
    assert not result.is_error
    assert "HASH-JOIN" in result.content[0].text


@pytest.mark.asyncio
async def test_redo_log_status_default_view():
    result = await call_tool(build_test_app(REDO_ARCHIVE_BACKUP_TOOLSET), "redo_log_status")
    assert not result.is_error
    assert "/u01/oradata/redo01a.log" in result.content[0].text


@pytest.mark.asyncio
async def test_archive_log_status_default_view():
    result = await call_tool(build_test_app(REDO_ARCHIVE_BACKUP_TOOLSET), "archive_log_status")
    assert not result.is_error
    assert '"last_archived_sequence": 48213' in result.content[0].text


@pytest.mark.asyncio
async def test_archive_pipeline_status_default_view():
    result = await call_tool(build_test_app(REDO_ARCHIVE_BACKUP_TOOLSET), "archive_pipeline_status")
    assert not result.is_error
    assert "LOG_ARCHIVE_DEST_1" in result.content[0].text


@pytest.mark.asyncio
async def test_backup_status_default_view():
    result = await call_tool(build_test_app(REDO_ARCHIVE_BACKUP_TOOLSET), "backup_status")
    assert not result.is_error
    assert '"session_key": 9821' in result.content[0].text


@pytest.mark.asyncio
async def test_ha_dr_status_default_check():
    """Full-protocol proof for the default call (check="dataguard_lag");
    restore_points/rac_health are unit-tested directly against
    get_query in test_high_availability_dr_query.py."""
    result = await call_tool(build_test_app(HIGH_AVAILABILITY_DR_TOOLSET), "ha_dr_status")
    assert not result.is_error
    assert "transport lag" in result.content[0].text


@pytest.mark.asyncio
async def test_database_services_default_view():
    """Full-protocol proof for the default call (view="active");
    definitions is unit-tested directly against get_services_query in
    test_high_availability_dr_query.py."""
    result = await call_tool(build_test_app(HIGH_AVAILABILITY_DR_TOOLSET), "database_services")
    assert not result.is_error
    assert "ebsprod" in result.content[0].text


@pytest.mark.asyncio
async def test_patch_history_default_view():
    """Full-protocol proof for the default call (view="applied_patches");
    product_versions is unit-tested directly against
    get_patch_history_query in test_patch_version_tracking_query.py."""
    result = await call_tool(build_test_app(PATCH_VERSION_TRACKING_TOOLSET), "patch_history")
    assert not result.is_error
    assert "991234" in result.content[0].text


@pytest.mark.asyncio
async def test_edition_inventory():
    result = await call_tool(build_test_app(PATCH_VERSION_TRACKING_TOOLSET), "edition_inventory")
    assert not result.is_error
    assert "ORA$BASE" in result.content[0].text


@pytest.mark.asyncio
async def test_adop_session_status_default_reports_active_session():
    result = await call_tool(build_test_app(PATCH_VERSION_TRACKING_TOOLSET), "adop_session_status")
    assert not result.is_error
    text = result.content[0].text
    assert '"is_active": true' in text
    assert '"current_phase": "apply"' in text
    assert "Session #47 is active" in text


@pytest.mark.asyncio
async def test_adop_session_status_by_id():
    result = await call_tool(
        build_test_app(PATCH_VERSION_TRACKING_TOOLSET), "adop_session_status", {"session_id": 47}
    )
    assert not result.is_error
    assert '"adop_session_id": 47' in result.content[0].text


@pytest.mark.asyncio
async def test_tablespace_health_default_view():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "tablespace_health")
    assert not result.is_error
    assert "APPS_TS_TX_DATA" in result.content[0].text


@pytest.mark.asyncio
async def test_temp_usage():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "temp_usage")
    assert not result.is_error
    assert '"used_mb": 4096.0' in result.content[0].text


@pytest.mark.asyncio
async def test_temp_usage_by_session():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "temp_usage_by_session")
    assert not result.is_error
    assert "9fkq9zbjw2vqm" in result.content[0].text


@pytest.mark.asyncio
async def test_undo_usage():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "undo_usage")
    assert not result.is_error
    assert '"undoblks": 18422' in result.content[0].text


@pytest.mark.asyncio
async def test_fra_usage():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "fra_usage")
    assert not result.is_error
    assert '"number_of_files": 842' in result.content[0].text


@pytest.mark.asyncio
async def test_top_segments_in_tablespace():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "top_segments_in_tablespace",
                              {"tablespace_name": "APPS_TS_TX_DATA"})
    assert not result.is_error
    assert "FND_LOBS" in result.content[0].text


@pytest.mark.asyncio
async def test_database_objects():
    """Default call — unchanged from before generalization: no filters,
    status defaults to INVALID. last_ddl_time has to reach the payload:
    it's what answers "when was this object last compiled"."""
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "database_objects")
    assert not result.is_error
    assert "XX_CUSTOM_PKG" in result.content[0].text
    assert "last_ddl_time" in result.content[0].text


@pytest.mark.asyncio
async def test_database_objects_lookup_by_name():
    """Full-protocol proof of the new lookup capability; bind discipline
    itself is unit-tested directly in test_database_objects_query.py."""
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET),
        "database_objects",
        {"object_name": "XX_CUSTOM_PKG", "status": "VALID"},
    )
    assert not result.is_error
    assert '"status": "INVALID"' in result.content[0].text


@pytest.mark.asyncio
async def test_database_objects_empty_result_names_the_status_filter():
    """A healthy object looked up by name alone matches nothing, because
    status still defaults to INVALID. The summary has to say so, or an
    empty result reads as "no such object". Empty fixture, because the
    substring-keyed mock can't make a bind filter miss."""
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET, canned={}),
        "database_objects",
        {"object_name": "XX_CUSTOM_PKG"},
    )
    assert not result.is_error
    assert "status=INVALID" in result.content[0].text
    assert "status=None" in result.content[0].text


@pytest.mark.asyncio
async def test_database_objects_no_status_filter_says_nothing_about_status():
    """status=None really did search everything, so the summary must not
    blame a filter that wasn't applied."""
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET, canned={}),
        "database_objects",
        {"object_name": "XX_CUSTOM_PKG", "status": None},
    )
    assert not result.is_error
    assert "No matching objects found" in result.content[0].text


@pytest.mark.asyncio
async def test_object_errors():
    """The other half of the INVALID question: database_objects says an
    object is broken, this says why. The ORA/PLS text and line number are
    the whole point, so both have to reach the payload."""
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "object_errors")
    assert not result.is_error
    assert "PLS-00201" in result.content[0].text
    assert '"line": 412' in result.content[0].text
    assert "1 object(s) with 2 error line(s)" in result.content[0].text


@pytest.mark.asyncio
async def test_object_errors_none_found_explains_dependency_invalidation():
    """No rows is the good-news case, and it has to say so — otherwise an
    empty payload reads as a failed lookup. Empty fixture, because the
    substring-keyed mock can't make a bind filter miss."""
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET, canned={}),
        "object_errors",
        {"object_name": "XX_CUSTOM_PKG"},
    )
    assert not result.is_error
    assert "No compilation errors recorded" in result.content[0].text
    assert "utlrp" in result.content[0].text


@pytest.mark.asyncio
async def test_blocking_locks():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "blocking_locks")
    assert not result.is_error
    assert '"blocking_sid": 44' in result.content[0].text


@pytest.mark.asyncio
async def test_top_sql_by_load():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "top_sql_by_load")
    assert not result.is_error
    assert '"executions": 412' in result.content[0].text


@pytest.mark.asyncio
async def test_stale_statistics():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "stale_statistics")
    assert not result.is_error
    assert "FND_CONCURRENT_REQUESTS" in result.content[0].text


@pytest.mark.asyncio
async def test_system_wait_profile():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "system_wait_profile")
    assert not result.is_error
    assert "db file sequential read" in result.content[0].text


@pytest.mark.asyncio
async def test_resource_limits():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "resource_limits")
    assert not result.is_error
    assert '"current_utilization": 340' in result.content[0].text


@pytest.mark.asyncio
async def test_alert_log_errors():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "alert_log_errors")
    assert not result.is_error
    assert "ORA-00060" in result.content[0].text


# --- Tier 1 additions (oracle-base.com/dba/scripts Monitoring inventory) ---


@pytest.mark.asyncio
async def test_unusable_indexes():
    result = await call_tool(build_test_app(INDEX_HEALTH_TOOLSET), "unusable_indexes")
    assert not result.is_error
    assert "XX_CUSTOM_IDX1" in result.content[0].text


@pytest.mark.asyncio
async def test_non_indexed_foreign_keys():
    result = await call_tool(build_test_app(INDEX_HEALTH_TOOLSET), "non_indexed_foreign_keys")
    assert not result.is_error
    assert "AP_INVOICES_N_FK1" in result.content[0].text


@pytest.mark.asyncio
async def test_db_component_registry():
    result = await call_tool(build_test_app(PATCH_VERSION_TRACKING_TOOLSET), "db_component_registry")
    assert not result.is_error
    assert "Oracle XML Database" in result.content[0].text


@pytest.mark.asyncio
async def test_db_privilege_grants_default_system_privs():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "db_privilege_grants")
    assert not result.is_error
    assert "SELECT ANY DICTIONARY" in result.content[0].text


@pytest.mark.asyncio
async def test_db_privilege_grants_object_privs():
    result = await call_tool(
        build_test_app(SECURITY_CONFIGURATION_TOOLSET), "db_privilege_grants", {"view": "object_privs"}
    )
    assert not result.is_error
    assert "XX_REPORT_USER" in result.content[0].text


@pytest.mark.asyncio
async def test_db_links_default_configured():
    result = await call_tool(build_test_app(SECURITY_CONFIGURATION_TOOLSET), "db_links")
    assert not result.is_error
    assert "GL_INTERFACE_LINK.WORLD" in result.content[0].text


@pytest.mark.asyncio
async def test_db_links_open():
    result = await call_tool(
        build_test_app(SECURITY_CONFIGURATION_TOOLSET), "db_links", {"view": "open"}
    )
    assert not result.is_error
    assert '"logged_on": "YES"' in result.content[0].text


@pytest.mark.asyncio
async def test_init_parameters_default_non_default():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "init_parameters")
    assert not result.is_error
    assert '"name": "processes"' in result.content[0].text


@pytest.mark.asyncio
async def test_init_parameters_diffs():
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET), "init_parameters", {"view": "diffs"}
    )
    assert not result.is_error
    assert "sga_target" in result.content[0].text


@pytest.mark.asyncio
async def test_undo_segment_detail():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "undo_segment_detail")
    assert not result.is_error
    assert "_SYSSMU12_123456789$" in result.content[0].text


@pytest.mark.asyncio
async def test_open_cursors_default_aggregate():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "open_cursors")
    assert not result.is_error
    assert '"open_cursor_count": 340' in result.content[0].text


@pytest.mark.asyncio
async def test_open_cursors_by_sid():
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET), "open_cursors", {"sid": 812}
    )
    assert not result.is_error
    assert "9fkq9zbjw2vqm" in result.content[0].text


@pytest.mark.asyncio
async def test_object_lock_inventory():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "object_lock_inventory")
    assert not result.is_error
    assert "AP_INVOICES_ALL" in result.content[0].text


@pytest.mark.asyncio
async def test_recyclebin():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "recyclebin")
    assert not result.is_error
    assert "XX_OLD_STAGING_TBL" in result.content[0].text


@pytest.mark.asyncio
async def test_dangling_synonyms():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "dangling_synonyms")
    assert not result.is_error
    assert "XX_OLD_SYNONYM" in result.content[0].text


@pytest.mark.asyncio
async def test_license_and_options_default_options():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "license_and_options")
    assert not result.is_error
    assert "Partitioning" in result.content[0].text


@pytest.mark.asyncio
async def test_license_and_options_license():
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET), "license_and_options", {"view": "license"}
    )
    assert not result.is_error
    assert '"sessions_current": 842' in result.content[0].text


@pytest.mark.asyncio
async def test_db_session_status_default_active():
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "db_session_status")
    assert not result.is_error
    assert '"sid": 812' in result.content[0].text
    assert '"status": "ACTIVE"' in result.content[0].text


@pytest.mark.asyncio
async def test_db_session_status_inactive():
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET), "db_session_status", {"status": "inactive"}
    )
    assert not result.is_error
    assert '"sid": 175' in result.content[0].text
    assert '"status": "INACTIVE"' in result.content[0].text


@pytest.mark.asyncio
async def test_db_session_status_all():
    result = await call_tool(
        build_test_app(INSTANCE_HEALTH_TOOLSET), "db_session_status", {"status": "all"}
    )
    assert not result.is_error
    text = result.content[0].text
    assert '"sid": 812' in text
    assert '"sid": 175' in text


@pytest.mark.asyncio
async def test_list_ebs_instances():
    """Full-protocol proof that list_ebs_instances is itself reachable and
    correctly shaped; the multi-instance intersection logic it delegates to
    (permitted_instances) is unit-tested directly in
    test_resolve_scoped_call.py — no need for a second build_test_app
    variant just to vary connectors here."""
    result = await call_tool(build_test_app(INSTANCE_HEALTH_TOOLSET), "list_ebs_instances")
    assert not result.is_error
    text = result.content[0].text
    assert '"instances": [\n    "TEST"\n  ]' in text
    assert '"auto_selected": "TEST"' in text
