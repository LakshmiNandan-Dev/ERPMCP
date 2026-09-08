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
    get_patch_history_query,
)


@pytest.mark.parametrize(
    "view,expected_table",
    [("applied_patches", "AD.AD_APPLIED_PATCHES"), ("product_versions", "APPLSYS.FND_PRODUCT_INSTALLATIONS")],
)
def test_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_patch_history_query(view)


@pytest.mark.parametrize("view", ["applied_patches", "product_versions"])
def test_every_view_passes_sql_conventions(view):
    validate_sql_conventions(get_patch_history_query(view))


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
