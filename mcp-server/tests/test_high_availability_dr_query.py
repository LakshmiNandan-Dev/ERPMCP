"""Direct unit tests of high_availability_dr.get_query — mirrors
test_redo_archive_backup_query.py exactly: proves each check selects the
right query and that all three respect the GV$/schema-qualification
convention, without needing a live Oracle connection.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.high_availability_dr import get_query, get_services_query


@pytest.mark.parametrize(
    "check,expected_table",
    [
        ("dataguard_lag", "GV$DATAGUARD_STATS"),
        ("restore_points", "GV$RESTORE_POINT"),
        ("rac_health", "GV$CLUSTER_INTERCONNECTS"),
    ],
)
def test_check_selects_the_right_query(check, expected_table):
    sql = get_query(check)
    assert expected_table in sql


@pytest.mark.parametrize("check", ["dataguard_lag", "restore_points", "rac_health"])
def test_every_check_passes_sql_conventions(check):
    validate_sql_conventions(get_query(check))


@pytest.mark.parametrize(
    "view,expected_table",
    [("active", "GV$ACTIVE_SERVICES"), ("definitions", "DBA_SERVICES")],
)
def test_services_view_selects_the_right_query(view, expected_table):
    sql = get_services_query(view)
    assert expected_table in sql


@pytest.mark.parametrize("view", ["active", "definitions"])
def test_services_query_passes_sql_conventions(view):
    validate_sql_conventions(get_services_query(view))
