"""Direct unit tests of instance_health.py's output-enrichment pure
functions — the pilot for making DBA tool output easier for an
assisting LLM (e.g. Copilot) to turn into a friendly answer: a per-row
severity flag plus a one-line summary, alongside the raw rows.
"""

from __future__ import annotations

from ebsmcp.tools.dba.instance_health import annotate_blocking_locks, annotate_tablespace_usage


def test_tablespace_usage_all_healthy():
    rows = [{"tablespace_name": "USERS", "pct_used": 40.0}]
    annotated, summary = annotate_tablespace_usage(rows)
    assert annotated == [{"tablespace_name": "USERS", "pct_used": 40.0, "severity": "ok"}]
    assert summary == "All tablespaces healthy (below 75% used)"


def test_tablespace_usage_warning_only():
    rows = [{"tablespace_name": "USERS", "pct_used": 80.0}]
    annotated, summary = annotate_tablespace_usage(rows)
    assert annotated[0]["severity"] == "warning"
    assert summary == "1 tablespace(s) at warning level (>=75% used)"


def test_tablespace_usage_critical_and_warning_mixed():
    rows = [
        {"tablespace_name": "SYSTEM", "pct_used": 95.0},
        {"tablespace_name": "USERS", "pct_used": 80.0},
        {"tablespace_name": "TOOLS", "pct_used": 10.0},
    ]
    annotated, summary = annotate_tablespace_usage(rows)
    assert [r["severity"] for r in annotated] == ["critical", "warning", "ok"]
    assert summary == "1 tablespace(s) critical (>=90% used), 1 at warning level (>=75%)"


def test_tablespace_usage_handles_missing_pct():
    rows = [{"tablespace_name": "WEIRD", "pct_used": None}]
    annotated, summary = annotate_tablespace_usage(rows)
    assert annotated[0]["severity"] == "ok"


def test_tablespace_usage_original_rows_not_mutated():
    row = {"tablespace_name": "USERS", "pct_used": 95.0}
    rows = [row]
    annotate_tablespace_usage(rows)
    assert "severity" not in row


def test_blocking_locks_empty_is_clean_summary():
    annotated, summary = annotate_blocking_locks([])
    assert annotated == []
    assert summary == "No blocking locks detected"


def test_blocking_locks_severity_and_longest_wait():
    rows = [
        {"blocking_sid": 1, "waiting_sid": 2, "seconds_in_wait": 400},
        {"blocking_sid": 1, "waiting_sid": 3, "seconds_in_wait": 90},
        {"blocking_sid": 1, "waiting_sid": 4, "seconds_in_wait": 5},
    ]
    annotated, summary = annotate_blocking_locks(rows)
    assert [r["severity"] for r in annotated] == ["critical", "warning", "ok"]
    assert summary == "3 session(s) blocked, longest wait 400s"
