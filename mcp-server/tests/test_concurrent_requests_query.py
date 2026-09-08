"""Direct unit tests of concurrent_processing.build_query — the pure
WHERE-clause/bind-construction function backing the concurrent_requests
tool. This is what actually varies per status/filter combination; the
tool's own resolve_scoped_call plumbing is identical to every other DBA
tool and is proven once, elsewhere (test_resolve_scoped_call.py,
test_instance_health_tool.py) — no need to re-prove it five more times
through the full MCP protocol just to check a WHERE clause.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.concurrent_processing import build_query, summarize_concurrent_requests


@pytest.mark.parametrize(
    "status,expected_fragment",
    [
        ("running", "fcr.phase_code = 'R'"),
        ("pending", "fcr.phase_code = 'P'"),
        ("on_hold", "fcr.phase_code = 'P' AND fcr.status_code = 'H'"),
        ("completed", "fcr.phase_code = 'C' AND fcr.status_code = 'C'"),
        ("failed", "fcr.phase_code = 'C' AND fcr.status_code = 'E'"),
    ],
)
def test_status_selects_the_right_condition(status, expected_fragment):
    sql, binds = build_query(status, requested_by=None, since=None)
    assert expected_fragment in sql
    assert binds == {}


def test_requested_by_is_bound_not_interpolated():
    sql, binds = build_query("running", requested_by="jdoe", since=None)
    assert "jdoe" not in sql  # never appears as literal text in the SQL
    assert ":requested_by" in sql
    assert binds["requested_by"] == "JDOE"  # EBS usernames are upper-case


def test_since_filters_by_start_date_for_non_completion_statuses():
    sql, binds = build_query("running", requested_by=None, since="2026-08-01")
    assert "fcr.actual_start_date >= TO_DATE(:since, 'YYYY-MM-DD')" in sql
    assert binds["since"] == "2026-08-01"


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_since_filters_by_completion_date_for_completion_statuses(status):
    sql, _binds = build_query(status, requested_by=None, since="2026-08-01")
    assert "fcr.actual_completion_date >= TO_DATE(:since, 'YYYY-MM-DD')" in sql
    assert "fcr.actual_start_date >= TO_DATE" not in sql


def test_combined_filters_all_bind_and_none_leak_into_sql_text():
    sql, binds = build_query("failed", requested_by="jdoe", since="2026-08-01")
    assert binds == {"requested_by": "JDOE", "since": "2026-08-01"}
    assert "jdoe" not in sql
    assert "2026-08-01" not in sql


def test_running_includes_session_correlation_and_elapsed_time():
    sql, _binds = build_query("running", requested_by=None, since=None)
    assert "LEFT JOIN GV$SESSION gs" in sql
    assert "gs.sid" in sql
    assert "gs.serial#" in sql
    assert "gs.status AS session_status" in sql
    assert "gs.sql_id" in sql
    assert "gs.event AS wait_event" in sql
    assert "elapsed_minutes" in sql


@pytest.mark.parametrize("status", ["pending", "on_hold", "completed", "failed"])
def test_non_running_statuses_skip_the_session_join_but_keep_the_shape(status):
    """The expensive/misleading part (correlating a live session) only
    makes sense for status="running" — see build_query's docstring. Every
    other status must still report the same 5 columns (as literal NULLs)
    so the result shape doesn't vary by status, and must NOT pay for the
    GV$SESSION join at all — verified live (2026-09-02): joining it
    unconditionally made an unfiltered "completed" query hang past 30s."""
    sql, _binds = build_query(status, requested_by=None, since=None)
    assert "GV$SESSION" not in sql
    assert "NULL AS sid" in sql
    assert "NULL AS serial#" in sql
    assert "NULL AS session_status" in sql
    assert "NULL AS sql_id" in sql
    assert "NULL AS wait_event" in sql
    assert "elapsed_minutes" in sql


def test_is_capped_and_passes_sql_conventions():
    sql, _binds = build_query("running", requested_by=None, since=None)
    assert "FETCH FIRST 50 ROWS ONLY" in sql
    validate_sql_conventions(sql)


def test_program_name_is_bound_not_interpolated():
    sql, binds = build_query("completed", requested_by=None, since=None, program_name="Create Accounting")
    assert "Create Accounting" not in sql
    assert "UPPER(fcpt.user_concurrent_program_name) = UPPER(:program_name)" in sql
    assert binds["program_name"] == "Create Accounting"
    validate_sql_conventions(sql)


def test_program_name_combines_with_since_for_a_history_window():
    sql, binds = build_query(
        "completed", requested_by=None, since="2026-08-26", program_name="Create Accounting"
    )
    assert binds == {"since": "2026-08-26", "program_name": "Create Accounting"}
    assert "fcr.actual_completion_date >= TO_DATE(:since, 'YYYY-MM-DD')" in sql
    assert "UPPER(fcpt.user_concurrent_program_name) = UPPER(:program_name)" in sql


def test_summarize_empty():
    assert summarize_concurrent_requests([], "completed") == "No completed requests found"


def test_summarize_no_elapsed_time_yet():
    rows = [{"elapsed_minutes": None}, {"elapsed_minutes": None}]
    assert summarize_concurrent_requests(rows, "pending") == (
        "2 pending request(s) found (no elapsed time available yet)"
    )


def test_summarize_computes_avg_min_max():
    rows = [{"elapsed_minutes": 5.0}, {"elapsed_minutes": 15.0}, {"elapsed_minutes": 10.0}]
    summary = summarize_concurrent_requests(rows, "completed")
    assert summary == "3 completed request(s) — avg 10.0 min, min 5.0 min, max 15.0 min"


def test_summarize_ignores_rows_missing_elapsed_time():
    rows = [{"elapsed_minutes": 8.0}, {"elapsed_minutes": None}]
    summary = summarize_concurrent_requests(rows, "completed")
    assert summary == "2 completed request(s) — avg 8.0 min, min 8.0 min, max 8.0 min"
