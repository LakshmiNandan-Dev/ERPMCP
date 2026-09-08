"""Direct unit tests of instance_health.build_object_errors_query — same
bind-discipline reasoning as test_database_objects_query.py: every filter
is bound and upper-cased, never interpolated, and the default
(attribute="ERROR") keeps PLSQL_WARNINGS noise out of the standard
post-patch triage call.
"""

from __future__ import annotations

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.instance_health import (
    build_object_errors_query,
    summarize_object_errors,
)


def test_default_call_filters_to_errors_only():
    sql, binds = build_object_errors_query(None, None, None, "ERROR")
    assert "attribute = :attribute" in sql
    assert binds == {"attribute": "ERROR"}
    validate_sql_conventions(sql)


def test_attribute_none_includes_warnings():
    sql, binds = build_object_errors_query(None, None, None, None)
    assert "WHERE" not in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_filters_are_bound_not_interpolated():
    sql, binds = build_object_errors_query("xx_custom_pkg", "package body", "apps", "ERROR")
    for literal in ("xx_custom_pkg", "package body", "apps", "'ERROR'"):
        assert literal not in sql
    assert binds == {
        "object_name": "XX_CUSTOM_PKG",
        "object_type": "PACKAGE BODY",
        "owner": "APPS",
        "attribute": "ERROR",
    }
    validate_sql_conventions(sql)


def test_all_filters_combine_with_and_under_one_where():
    sql, _ = build_object_errors_query("xx_custom_pkg", "package body", "apps", "ERROR")
    assert sql.count("WHERE") == 1
    assert sql.count(" AND ") == 3
    validate_sql_conventions(sql)


def test_query_is_always_row_capped():
    """A single broken package body can emit dozens of lines; the cap is
    not conditional on any filter being supplied."""
    for args in [(None, None, None, None), ("xx_custom_pkg", None, None, "ERROR")]:
        sql, _ = build_object_errors_query(*args)
        assert "FETCH FIRST 100 ROWS ONLY" in sql


def test_summary_of_no_errors_explains_dependency_invalidation():
    """The empty case is the diagnostic one: INVALID with no errors means
    invalidated, not broken."""
    summary = summarize_object_errors([], "ERROR")
    assert "attribute=ERROR" in summary
    assert "dependency" in summary
    assert "utlrp" in summary


def test_summary_counts_distinct_objects_not_just_rows():
    rows = [
        {"owner": "APPS", "name": "XX_A", "type": "PACKAGE BODY", "line": 1},
        {"owner": "APPS", "name": "XX_A", "type": "PACKAGE BODY", "line": 2},
        {"owner": "APPS", "name": "XX_B", "type": "PACKAGE BODY", "line": 9},
    ]
    assert summarize_object_errors(rows, "ERROR") == "2 object(s) with 3 error line(s)"


def test_summary_flags_truncation_at_the_cap():
    rows = [
        {"owner": "APPS", "name": "XX_A", "type": "PACKAGE BODY", "line": n}
        for n in range(100)
    ]
    summary = summarize_object_errors(rows, "ERROR")
    assert "truncated at 100 rows" in summary
    assert "narrow by" in summary
