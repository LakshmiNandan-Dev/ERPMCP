"""Direct unit tests of the Tier 1 (oracle-base.com Monitoring inventory)
additions to instance_health.py: get_init_parameters_query,
build_open_cursors_query, get_license_and_options_query. Mirrors
test_high_availability_dr_query.py / test_security_configuration_queries.py.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.instance_health import (
    build_open_cursors_query,
    get_db_session_status_query,
    get_init_parameters_query,
    get_license_and_options_query,
)


@pytest.mark.parametrize(
    "view,expected",
    [("non_default", "isdefault = 'FALSE'"), ("diffs", "HAVING COUNT(DISTINCT value) > 1")],
)
def test_init_parameters_view_selects_the_right_query(view, expected):
    assert expected in get_init_parameters_query(view)


@pytest.mark.parametrize("view", ["non_default", "diffs"])
def test_init_parameters_query_passes_sql_conventions(view):
    validate_sql_conventions(get_init_parameters_query(view))


def test_open_cursors_without_sid_aggregates_by_session():
    sql, binds = build_open_cursors_query(None)
    assert "GROUP BY inst_id, sid, user_name" in sql
    assert "WHERE" not in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_open_cursors_with_sid_is_bound_not_interpolated():
    sql, binds = build_open_cursors_query(812)
    assert "sid = :sid" in sql
    assert "812" not in sql
    assert binds == {"sid": 812}
    validate_sql_conventions(sql)


@pytest.mark.parametrize(
    "view,expected_table",
    [("options", "GV$OPTION"), ("license", "GV$LICENSE")],
)
def test_license_and_options_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_license_and_options_query(view)


@pytest.mark.parametrize("view", ["options", "license"])
def test_license_and_options_query_passes_sql_conventions(view):
    validate_sql_conventions(get_license_and_options_query(view))


@pytest.mark.parametrize(
    "status,expected",
    [
        ("active", "status = 'ACTIVE' AND type = 'USER'"),
        ("inactive", "status = 'INACTIVE' AND type = 'USER'"),
        ("all", "WHERE type = 'USER'"),
    ],
)
def test_db_session_status_selects_the_right_query(status, expected):
    assert expected in get_db_session_status_query(status)


@pytest.mark.parametrize("status", ["active", "inactive", "all"])
def test_db_session_status_query_is_capped_and_passes_sql_conventions(status):
    sql = get_db_session_status_query(status)
    assert "FETCH FIRST 50 ROWS ONLY" in sql
    validate_sql_conventions(sql)
