"""Direct unit tests of workflow.build_query — same reasoning as
test_concurrent_requests_query.py: this is what actually varies per
status/filter combination; resolve_scoped_call's plumbing is proven
elsewhere.
"""

from __future__ import annotations

import pytest

from ebsmcp.tools.dba.workflow import build_query


@pytest.mark.parametrize(
    "status,expected_fragment",
    [
        ("error", "wias.activity_status = 'ERROR'"),
        ("active", "wias.activity_status = 'ACTIVE'"),
        ("suspended", "wias.activity_status = 'SUSPEND'"),
        ("deferred", "wias.activity_status = 'DEFERRED'"),
    ],
)
def test_status_selects_the_right_condition(status, expected_fragment):
    sql, binds = build_query(status, item_type=None, since=None)
    assert expected_fragment in sql
    assert binds == {}


def test_item_type_is_bound_not_interpolated():
    sql, binds = build_query("error", item_type="poapprv", since=None)
    assert "poapprv" not in sql
    assert ":item_type" in sql
    assert binds["item_type"] == "POAPPRV"


def test_since_is_bound():
    sql, binds = build_query("error", item_type=None, since="2026-08-01")
    assert "wias.begin_date >= TO_DATE(:since, 'YYYY-MM-DD')" in sql
    assert binds["since"] == "2026-08-01"


def test_combined_filters_all_bind_and_none_leak_into_sql_text():
    sql, binds = build_query("active", item_type="poapprv", since="2026-08-01")
    assert binds == {"item_type": "POAPPRV", "since": "2026-08-01"}
    assert "poapprv" not in sql
    assert "2026-08-01" not in sql
