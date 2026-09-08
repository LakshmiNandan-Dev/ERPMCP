"""Direct unit tests of instance_health.build_database_objects_query —
same bind-discipline reasoning as test_concurrent_requests_query.py:
object_name/object_type/status are always bound, never interpolated,
and the default (status="INVALID", nothing else) matches the original
invalid_objects behavior exactly.
"""

from __future__ import annotations

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.instance_health import build_database_objects_query


def test_default_call_filters_to_invalid_only():
    sql, binds = build_database_objects_query(None, None, "INVALID")
    assert "status = :status" in sql
    assert binds == {"status": "INVALID"}
    validate_sql_conventions(sql)


def test_status_none_means_every_status():
    sql, binds = build_database_objects_query(None, None, None)
    assert "WHERE" not in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_object_name_is_bound_not_interpolated():
    sql, binds = build_database_objects_query("xx_custom_pkg", None, None)
    assert "xx_custom_pkg" not in sql
    assert binds == {"object_name": "XX_CUSTOM_PKG"}
    validate_sql_conventions(sql)


def test_object_type_is_bound_not_interpolated():
    sql, binds = build_database_objects_query(None, "package body", None)
    assert "package body" not in sql
    assert binds == {"object_type": "PACKAGE BODY"}
    validate_sql_conventions(sql)


def test_status_valid_overrides_default_invalid_filter():
    """Confirms a specific object recompiled successfully — the
    status="VALID" use case the tool's docstring describes."""
    sql, binds = build_database_objects_query("xx_custom_pkg", None, "VALID")
    assert binds == {"object_name": "XX_CUSTOM_PKG", "status": "VALID"}
    assert "xx_custom_pkg" not in sql
    assert "VALID" not in sql  # bound, not interpolated — "status = :status" only
    validate_sql_conventions(sql)


def test_all_three_filters_combine_with_and():
    sql, binds = build_database_objects_query("xx_custom_pkg", "package body", "VALID")
    assert sql.count("WHERE") == 1
    assert " AND " in sql
    assert binds == {
        "object_name": "XX_CUSTOM_PKG",
        "object_type": "PACKAGE BODY",
        "status": "VALID",
    }
    validate_sql_conventions(sql)
