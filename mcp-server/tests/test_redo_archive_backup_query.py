"""Direct unit tests of redo_archive_backup's four get_*_query functions
— proves each view selects the right query and that every one respects
the GV$/schema-qualification convention, without needing a live Oracle
connection.
"""

from __future__ import annotations

import pytest

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.redo_archive_backup import (
    get_archive_log_query,
    get_archive_pipeline_query,
    get_backup_query,
    get_redo_log_query,
)


@pytest.mark.parametrize(
    "view,expected_table",
    [("current", "GV$LOGFILE"), ("switch_history", "GV$LOG_HISTORY")],
)
def test_redo_log_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_redo_log_query(view)


@pytest.mark.parametrize(
    "view,expected_fragment",
    [("gaps", "GV$ARCHIVED_LOG"), ("generation_rate", "AS volume_mb")],
)
def test_archive_log_view_selects_the_right_query(view, expected_fragment):
    assert expected_fragment in get_archive_log_query(view)


@pytest.mark.parametrize(
    "view,expected_table",
    [("destinations", "GV$ARCHIVE_DEST_STATUS"), ("processes", "GV$ARCHIVE_PROCESSES")],
)
def test_archive_pipeline_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_archive_pipeline_query(view)


@pytest.mark.parametrize(
    "view,expected_table",
    [
        ("jobs", "GV$RMAN_BACKUP_JOB_DETAILS"),
        ("by_datafile", "GV$BACKUP_DATAFILE"),
        ("archivelog_coverage", "GV$ARCHIVED_LOG"),
        ("controlfile", "GV$BACKUP_CONTROLFILE_SUMMARY"),
        ("retention_compliance", "GV$RMAN_CONFIGURATION"),
    ],
)
def test_backup_view_selects_the_right_query(view, expected_table):
    assert expected_table in get_backup_query(view)


@pytest.mark.parametrize("view", ["current", "switch_history"])
def test_redo_log_query_passes_sql_conventions(view):
    validate_sql_conventions(get_redo_log_query(view))


@pytest.mark.parametrize("view", ["gaps", "generation_rate"])
def test_archive_log_query_passes_sql_conventions(view):
    validate_sql_conventions(get_archive_log_query(view))


@pytest.mark.parametrize("view", ["destinations", "processes"])
def test_archive_pipeline_query_passes_sql_conventions(view):
    validate_sql_conventions(get_archive_pipeline_query(view))


@pytest.mark.parametrize(
    "view", ["jobs", "by_datafile", "archivelog_coverage", "controlfile", "retention_compliance"]
)
def test_backup_query_passes_sql_conventions(view):
    validate_sql_conventions(get_backup_query(view))
