"""EBS database connector: OracleEBSConnector for a real instance,
MockEBSConnector so the rest of the pipeline is runnable and testable
before any real EBS credentials exist.

OracleEBSConnector uses python-oracledb in thin mode (no Oracle Instant
Client install required) and opens one connection per call for this first
slice — a pooled connection (oracledb.create_pool) is the obvious upgrade
once this is under real load, but isn't needed to prove the pattern.
"""

from __future__ import annotations

from typing import Any

from ebsmcp.connectors.base import EBSConnector


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
