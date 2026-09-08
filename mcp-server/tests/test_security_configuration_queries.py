"""Direct unit tests of security_configuration.py's pure query-building
functions — db_level_accounts' view selection (mirrors
test_high_availability_dr_query.py), plus the three bind-discipline
functions (mirrors test_concurrent_requests_query.py): free-text filter
values must always be bound, never interpolated.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.security_configuration import (
    RESPONSIBILITY_LEVEL_ID,
    build_db_privilege_grants_query,
    build_failed_login_query,
    build_profile_values_query,
    build_responsibility_assignments_query,
    get_db_accounts_query,
    get_db_links_query,
    get_login_sessions_query,
    get_named_user_count_query,
)


@pytest.mark.parametrize(
    "view,expected_table",
    [("accounts", "DBA_USERS"), ("privileged_roles", "DBA_ROLE_PRIVS")],
)
def test_db_accounts_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_db_accounts_query(view)


@pytest.mark.parametrize("view", ["accounts", "privileged_roles"])
def test_db_accounts_query_passes_sql_conventions(view):
    validate_sql_conventions(get_db_accounts_query(view))


def test_failed_login_query_without_username_is_fleet_wide():
    sql, binds = build_failed_login_query(None)
    assert "login_name = :username" not in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_failed_login_query_username_is_bound_not_interpolated():
    sql, binds = build_failed_login_query("jdoe")
    assert "jdoe" not in sql
    assert binds == {"username": "JDOE"}
    validate_sql_conventions(sql)


def test_responsibility_assignments_with_no_filters_has_no_where():
    sql, binds = build_responsibility_assignments_query(None, None)
    assert "WHERE" not in sql
    assert "FETCH FIRST 200 ROWS ONLY" in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_responsibility_assignments_forward_lookup_binds_username():
    sql, binds = build_responsibility_assignments_query("jdoe", None)
    assert "jdoe" not in sql
    assert binds == {"username": "JDOE"}
    validate_sql_conventions(sql)


def test_responsibility_assignments_reverse_lookup_binds_responsibility_name():
    sql, binds = build_responsibility_assignments_query(None, "System Administrator")
    assert "System Administrator" not in sql
    assert binds == {"responsibility_name": "System Administrator"}
    validate_sql_conventions(sql)


def test_profile_values_without_responsibility_id_has_no_level_filter():
    sql, binds = build_profile_values_query("MO_OPERATING_UNIT", None)
    assert "fpov.level_id = :level_id" not in sql
    assert binds == {"profile_option_name": "MO_OPERATING_UNIT"}
    validate_sql_conventions(sql)


def test_profile_values_with_responsibility_id_uses_responsibility_level_id():
    sql, binds = build_profile_values_query("MO_OPERATING_UNIT", 20420)
    assert "fpov.level_id = :level_id" in sql
    assert binds == {
        "profile_option_name": "MO_OPERATING_UNIT",
        "level_id": RESPONSIBILITY_LEVEL_ID,
        "responsibility_id": 20420,
    }
    validate_sql_conventions(sql)


@pytest.mark.parametrize(
    "view,expected_table", [("system_privs", "DBA_SYS_PRIVS"), ("object_privs", "DBA_TAB_PRIVS")]
)
def test_db_privilege_grants_view_selects_the_right_query(view, expected_table):
    sql, binds = build_db_privilege_grants_query(view, None)
    assert expected_table in sql
    assert "WHERE" not in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_db_privilege_grants_grantee_is_bound_not_interpolated():
    sql, binds = build_db_privilege_grants_query("system_privs", "xx_custom_role")
    assert "grantee = :grantee" in sql
    assert "xx_custom_role" not in sql
    assert binds == {"grantee": "XX_CUSTOM_ROLE"}
    validate_sql_conventions(sql)


@pytest.mark.parametrize(
    "view,expected_table", [("configured", "DBA_DB_LINKS"), ("open", "GV$DBLINK")]
)
def test_db_links_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_db_links_query(view)


@pytest.mark.parametrize("view", ["configured", "open"])
def test_db_links_query_passes_sql_conventions(view):
    validate_sql_conventions(get_db_links_query(view))


@pytest.mark.parametrize(
    "view,expected", [("active", "end_time IS NULL"), ("closed", "end_time IS NOT NULL")]
)
def test_login_sessions_view_selects_the_right_query(view, expected):
    assert expected in get_login_sessions_query(view)


@pytest.mark.parametrize("view", ["active", "closed"])
def test_login_sessions_query_is_capped_and_passes_sql_conventions(view):
    sql = get_login_sessions_query(view)
    assert "FETCH FIRST 50 ROWS ONLY" in sql
    validate_sql_conventions(sql)


@pytest.mark.parametrize(
    "status,expected",
    [("active", "end_date IS NULL OR"), ("inactive", "end_date IS NOT NULL AND")],
)
def test_named_user_count_status_selects_the_right_query(status, expected):
    assert expected in get_named_user_count_query(status)


@pytest.mark.parametrize("status", ["active", "inactive"])
def test_named_user_count_query_passes_sql_conventions(status):
    validate_sql_conventions(get_named_user_count_query(status))
