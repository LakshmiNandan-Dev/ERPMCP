import pytest

from ebsmcp.connectors.base import UnqualifiedSQLError, validate_sql_conventions


def test_fully_qualified_gv_dollar_passes():
    validate_sql_conventions("SELECT * FROM SYS.GV$SESSION WHERE status = :status")


def test_fully_qualified_application_schema_passes():
    validate_sql_conventions(
        "SELECT request_id FROM APPS.FND_CONCURRENT_REQUESTS WHERE org_id = :org_id"
    )


def test_dual_is_exempt_from_qualification():
    validate_sql_conventions("SELECT 1 FROM DUAL")


def test_join_targets_are_also_checked():
    with pytest.raises(UnqualifiedSQLError):
        validate_sql_conventions(
            "SELECT a.request_id FROM APPS.FND_CONCURRENT_REQUESTS a "
            "JOIN FND_CONCURRENT_PROCESSES b ON a.controlling_manager = b.concurrent_process_id"
        )


def test_bare_v_dollar_is_rejected():
    with pytest.raises(UnqualifiedSQLError, match="GV\\$"):
        validate_sql_conventions("SELECT * FROM SYS.V$SESSION")


def test_unqualified_object_is_rejected():
    with pytest.raises(UnqualifiedSQLError, match="unqualified"):
        validate_sql_conventions("SELECT * FROM FND_CONCURRENT_REQUESTS")


def test_bare_gv_dollar_is_exempt_from_qualification():
    """2026-09-02 revision: bare GV$*/DBA_* resolve via Oracle's standard
    public synonyms, and the SYS.-qualified form was found to actually
    fail even for an account with adequate underlying privilege — see
    connectors/base.py's module docstring."""
    validate_sql_conventions("SELECT * FROM GV$SESSION WHERE status = :status")


def test_bare_dba_is_exempt_from_qualification():
    validate_sql_conventions("SELECT * FROM DBA_USERS")


def test_bare_v_dollar_is_still_rejected_even_unqualified():
    """Only GV$ is exempt — bare V$ (single-instance-only, non-portable
    to RAC) is still rejected regardless of SYS. qualification."""
    with pytest.raises(UnqualifiedSQLError, match="GV\\$"):
        validate_sql_conventions("SELECT * FROM V$SESSION")


def test_other_unqualified_objects_still_rejected_alongside_exempt_ones():
    """The exemption is narrowly scoped to GV$*/DBA_* — a query mixing an
    exempt catalog view with a real, non-exempt product-schema table
    still requires the latter to be qualified."""
    with pytest.raises(UnqualifiedSQLError, match="unqualified"):
        validate_sql_conventions(
            "SELECT * FROM GV$SESSION s JOIN FND_CONCURRENT_REQUESTS r ON r.oracle_process_id = s.process"
        )
