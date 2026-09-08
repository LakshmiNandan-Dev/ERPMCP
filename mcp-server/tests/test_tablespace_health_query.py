"""Direct unit tests of instance_health.get_tablespace_query — mirrors
test_high_availability_dr_query.py: proves each view selects the right
query and that all three respect the GV$/schema-qualification
convention, without needing a live Oracle connection.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.instance_health import get_tablespace_query


@pytest.mark.parametrize(
    "view,expected_fragment",
    [
        ("usage", "DBA_DATA_FILES"),
        ("datafile_headroom", "AS pct_of_max"),
        ("status", "DBA_TABLESPACES"),
    ],
)
def test_view_selects_the_right_query(view, expected_fragment):
    sql = get_tablespace_query(view)
    assert expected_fragment in sql


@pytest.mark.parametrize("view", ["usage", "datafile_headroom", "status"])
def test_every_view_passes_sql_conventions(view):
    validate_sql_conventions(get_tablespace_query(view))
