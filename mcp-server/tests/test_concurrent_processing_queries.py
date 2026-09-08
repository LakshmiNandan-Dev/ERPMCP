"""Direct unit tests of manager_capacity/concurrent_load_trend's query
dicts — proves each enum value selects the right query and that every
one respects the GV$/schema-qualification convention, without needing a
live Oracle connection. concurrent_requests has its own
test_concurrent_requests_query.py already, for its bind-variable logic.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.concurrent_processing import get_load_trend_query, get_manager_capacity_query


@pytest.mark.parametrize(
    "view,expected_fragment",
    [
        ("target_vs_actual", "fcq.max_processes AS target_processes"),
        ("by_worker", "fcp.oracle_process_id"),
    ],
)
def test_manager_capacity_view_selects_the_right_query(view, expected_fragment):
    sql = get_manager_capacity_query(view)
    assert expected_fragment in sql


@pytest.mark.parametrize(
    "group_by,expected_fragment",
    [
        ("program", "GROUP BY fcpt.user_concurrent_program_name"),
        ("hour", "GROUP BY TRUNC(fcr.actual_completion_date, 'HH24')"),
    ],
)
def test_load_trend_group_by_selects_the_right_query(group_by, expected_fragment):
    sql = get_load_trend_query(group_by)
    assert expected_fragment in sql


@pytest.mark.parametrize("view", ["target_vs_actual", "by_worker"])
def test_manager_capacity_passes_sql_conventions(view):
    validate_sql_conventions(get_manager_capacity_query(view))


@pytest.mark.parametrize("group_by", ["program", "hour"])
def test_load_trend_passes_sql_conventions(group_by):
    validate_sql_conventions(get_load_trend_query(group_by))
