"""Direct unit tests of patch_version_tracking.get_patch_history_query —
mirrors test_high_availability_dr_query.py: proves each view selects the
right query and that both respect the GV$/schema-qualification
convention, without needing a live Oracle connection. Also covers
build_adop_session_query and annotate_adop_sessions (mirrors
test_instance_health_enrichment.py for the enrichment half).
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.patch_version_tracking import (
    annotate_adop_sessions,
    build_adop_session_query,
    build_patch_history_query,
)


@pytest.mark.parametrize(
    "view,expected_table",
    [("applied_patches", "APPS.AD_APPLIED_PATCHES"),
     ("product_versions", "APPS.FND_PRODUCT_INSTALLATIONS")],
)
def test_view_selects_the_right_query(view, expected_table):
    sql, _ = build_patch_history_query(view)
    assert expected_table in sql


@pytest.mark.parametrize("view", ["applied_patches", "product_versions"])
def test_every_view_passes_sql_conventions(view):
    sql, _ = build_patch_history_query(view)
    validate_sql_conventions(sql)


@pytest.mark.parametrize("view", ["applied_patches", "product_versions"])
def test_windowed_query_still_passes_sql_conventions(view):
    sql, _ = build_patch_history_query(view, days=30)
    validate_sql_conventions(sql)


def test_no_window_returns_most_recent_without_filtering_by_date():
    sql, binds = build_patch_history_query("applied_patches")
    assert "WHERE" not in sql
    assert binds == {}
    assert "FETCH FIRST 25 ROWS ONLY" in sql


@pytest.mark.parametrize("days", [1, 10, 30])
def test_window_is_a_rolling_range_and_is_bound_not_interpolated(days):
    """days=1 has to mean the last 24 hours. A YYYY-MM-DD floor could not
    express that — the floor for today is midnight today."""
    sql, binds = build_patch_history_query("applied_patches", days=days)
    assert "aap.creation_date >= SYSDATE - :days" in sql
    assert binds == {"days": days}
    assert str(days) not in sql.split("FETCH FIRST")[0].replace(":days", "")


def test_window_raises_the_row_cap_so_a_busy_month_is_not_truncated_at_25():
    sql, _ = build_patch_history_query("applied_patches", days=30)
    assert "FETCH FIRST 200 ROWS ONLY" in sql


def test_applied_patches_does_not_join_ad_bugs():
    """Joining AD_BUGS fans each patch out across every bug it delivers —
    the same duplication defect fixed in concurrent_requests."""
    sql, _ = build_patch_history_query("applied_patches")
    assert "AD_BUGS" not in sql
    assert "patch_name AS patch_number" in sql


def test_product_versions_ignores_the_window():
    """A window is meaningless for "what version is each product at" — that
    is current state, not history."""
    sql, binds = build_patch_history_query("product_versions", days=30)
    assert "SYSDATE" not in sql
    assert binds == {}


def test_adop_session_query_without_id_fetches_recent_five():
    sql, binds = build_adop_session_query(None)
    assert "WHERE" not in sql
    assert "FETCH FIRST 5 ROWS ONLY" in sql
    assert binds == {}
    assert "APPLSYS.AD_ADOP_SESSIONS" in sql
    validate_sql_conventions(sql)


def test_adop_session_query_with_id_is_bound_not_interpolated():
    sql, binds = build_adop_session_query(46)
    assert "adop_session_id = :session_id" in sql
    assert "FETCH FIRST" not in sql
    assert binds == {"session_id": 46}
    validate_sql_conventions(sql)


def test_annotate_adop_sessions_empty():
    annotated, summary = annotate_adop_sessions([])
    assert annotated == []
    assert summary == "No ADOP sessions found"


def test_annotate_adop_sessions_all_completed_reports_latest():
    rows = [
        {"adop_session_id": 46, "status": "C", "prepare_status": "X", "apply_status": "P",
         "finalize_status": "X", "cutover_status": "X", "cleanup_status": "N"},
        {"adop_session_id": 45, "status": "C", "prepare_status": "Y", "apply_status": "Y",
         "finalize_status": "Y", "cutover_status": "Y", "cleanup_status": "Y"},
    ]
    annotated, summary = annotate_adop_sessions(rows)
    assert all(not r["is_active"] for r in annotated)
    assert all(r["current_phase"] is None for r in annotated)
    assert summary == "No ADOP session currently active — most recent is #46 (status: C)"


def test_annotate_adop_sessions_active_reports_current_phase():
    rows = [
        {"adop_session_id": 47, "status": "R", "prepare_status": "Y", "apply_status": "R",
         "finalize_status": "N", "cutover_status": "N", "cleanup_status": "N"},
    ]
    annotated, summary = annotate_adop_sessions(rows)
    assert annotated[0]["is_active"] is True
    assert annotated[0]["current_phase"] == "apply"
    assert summary == "Session #47 is active — currently in apply"


def test_annotate_adop_sessions_active_between_phases():
    rows = [
        {"adop_session_id": 48, "status": "R", "prepare_status": "Y", "apply_status": "Y",
         "finalize_status": "N", "cutover_status": "N", "cleanup_status": "N"},
    ]
    annotated, summary = annotate_adop_sessions(rows)
    assert annotated[0]["is_active"] is True
    assert annotated[0]["current_phase"] is None
    assert summary == "Session #48 is active — currently in between phases"
