"""EBS database connector: OracleEBSConnector for a real instance,
MockEBSConnector so the rest of the pipeline is runnable and testable
before any real EBS credentials exist.

OracleEBSConnector uses python-oracledb, which defaults to THIN mode (no
Oracle Instant Client needed). Thin mode cannot authenticate against an
account the server presents with the old 10g password verifier — which is
what an EBS database with SEC_CASE_SENSITIVE_LOGON=FALSE does for every
account, regardless of any 11g/12c verifiers also present (thin mode then
fails with DPY-3015). Enabling THICK mode (EBSMCP_ORACLE_THICK_MODE=true,
which needs the Instant Client libraries in the image) handles that path.
init_thick_mode_if_configured() is called once at startup; it is a no-op
in the default thin configuration, so the mock and the test suite are
unaffected.

Opens one connection per call for this first slice — a pooled connection
(oracledb.create_pool) is the obvious upgrade under real load.
"""

from __future__ import annotations

from typing import Any

from ebsmcp.connectors.base import EBSConnector

_thick_mode_initialized = False


def init_thick_mode_if_configured(enabled: bool) -> None:
    """Switch python-oracledb into thick mode once per process, if asked.

    Thick mode is a global, one-time process setting: oracledb.init_oracle_client()
    must run before the first connection and exactly once. Guarded by a
    module flag so repeated calls (or multiple connectors) are safe. A no-op
    when `enabled` is false, which is the default — so thin mode, the mock,
    and the test suite are untouched unless a deployment explicitly opts in.

    Raises at startup (not per call) with a clear message if thick mode is
    requested but the Instant Client libraries are not present in the image.
    """
    global _thick_mode_initialized
    if not enabled or _thick_mode_initialized:
        return
    import oracledb

    try:
        oracledb.init_oracle_client()
    except Exception as exc:  # noqa: BLE001 — surface a deployment-config error clearly
        raise RuntimeError(
            "EBSMCP_ORACLE_THICK_MODE is set but python-oracledb could not initialize "
            "thick mode. The Oracle Instant Client libraries must be present in the "
            f"image (see mcp-server/Dockerfile). Underlying error: {exc}"
        ) from exc
    _thick_mode_initialized = True


class OracleEBSConnector(EBSConnector):
    def __init__(self, dsn: str, user: str, password: str) -> None:
        self._dsn = dsn
        self._user = user
        self._password = password

    def execute_query(self, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        import oracledb

        with oracledb.connect(user=self._user, password=self._password, dsn=self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, binds or {})
                columns = [col[0].lower() for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]


class MockEBSConnector(EBSConnector):
    """Returns canned rows keyed by a substring of the SQL, so a placeholder
    tool (or a test) can exercise the full request pipeline — identity
    resolution, entitlement filtering, audit logging — without a real
    Oracle database. Real DBA tools will need real fixtures; this exists
    only to prove the wiring works end to end.
    """

    def __init__(self, canned_responses: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._canned = canned_responses or {}

    def execute_query(self, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        for key, rows in self._canned.items():
            if key.lower() in sql.lower():
                return rows
        return []
