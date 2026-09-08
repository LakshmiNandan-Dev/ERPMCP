"""Direct unit tests of memory.py's get_sga_query/get_pga_query/
get_cache_query — mirrors test_high_availability_dr_query.py: proves
each view/cache selects the right query and that every one respects the
GV$/schema-qualification convention, without needing a live Oracle
connection.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.memory import get_cache_query, get_pga_query, get_sga_query


@pytest.mark.parametrize(
    "view,expected_fragment",
    [("overview", "GV$SGAINFO"), ("pool_detail", "GV$SGASTAT")],
)
def test_sga_view_selects_the_right_query(view, expected_fragment):
    assert expected_fragment in get_sga_query(view)


@pytest.mark.parametrize(
    "view,expected_fragment",
    [("overview", "'over allocation count'"), ("by_session", "GV$PROCESS")],
)
def test_pga_view_selects_the_right_query(view, expected_fragment):
    assert expected_fragment in get_pga_query(view)


@pytest.mark.parametrize(
    "cache,expected_table",
    [("library", "GV$LIBRARYCACHE"), ("buffer", "GV$BUFFER_POOL_STATISTICS")],
)
def test_cache_type_selects_the_right_query(cache, expected_table):
    assert expected_table in get_cache_query(cache)


@pytest.mark.parametrize("view", ["overview", "pool_detail"])
def test_sga_query_passes_sql_conventions(view):
    validate_sql_conventions(get_sga_query(view))


@pytest.mark.parametrize("view", ["overview", "by_session"])
def test_pga_query_passes_sql_conventions(view):
    validate_sql_conventions(get_pga_query(view))


@pytest.mark.parametrize("cache", ["library", "buffer"])
def test_cache_query_passes_sql_conventions(cache):
    validate_sql_conventions(get_cache_query(cache))
