"""Direct unit tests of workflow.build_notifications_query — same
reasoning as test_concurrent_requests_query.py: proves the
detail-vs-summary branching and bind discipline without needing the
full MCP protocol.
"""

from __future__ import annotations

from ebsmcp.connectors.base import validate_sql_conventions
from ebsmcp.tools.dba.workflow import build_notifications_query


def test_notification_id_given_returns_single_row_detail_lookup():
    sql, binds = build_notifications_query(notification_id=88231, mail_status=None)
    assert "WHERE wn.notification_id = :notification_id" in sql
    assert binds == {"notification_id": 88231}
    validate_sql_conventions(sql)


def test_notification_id_omitted_returns_aggregate_grouped_by_mail_status():
    sql, binds = build_notifications_query(notification_id=None, mail_status=None)
    assert "GROUP BY wn.mail_status" in sql
    assert "WHERE" not in sql
    assert binds == {}
    validate_sql_conventions(sql)


def test_mail_status_filter_is_bound_not_interpolated():
    sql, binds = build_notifications_query(notification_id=None, mail_status="failed")
    assert "failed" not in sql
    assert "WHERE wn.mail_status = :mail_status" in sql
    assert binds == {"mail_status": "FAILED"}
    validate_sql_conventions(sql)


def test_notification_id_takes_precedence_over_mail_status():
    """If both are given, the single-ID detail lookup wins — mail_status
    only applies to the aggregate view."""
    sql, binds = build_notifications_query(notification_id=88231, mail_status="failed")
    assert "wn.notification_id = :notification_id" in sql
    assert "mail_status" not in binds
