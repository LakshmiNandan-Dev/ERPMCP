"""Direct unit tests of the SCM build_* query functions — what actually
varies per filter combination. Shared properties (org scoping, bound values,
row cap, SQL conventions) are asserted for every query in one place.
"""

from __future__ import annotations

import json

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import (
    INVENTORY_TOOLSET,
    ORDER_MANAGEMENT_TOOLSET,
    PURCHASING_TOOLSET,
    RECEIVING_TOOLSET,
    SHIPPING_TOOLSET,
)
from ebsmcp.tools import ToolContext, mount_toolsets
from ebsmcp.tools.scm._scope import escape_like, org_in_clause
from ebsmcp.tools.scm.inventory import (
    build_item_lookup_query,
    build_onhand_query,
    build_transaction_errors_query,
)
from ebsmcp.tools.scm.order_management import (
    build_order_import_errors_query,
    build_order_query,
    build_orders_on_hold_query,
)
from ebsmcp.tools.scm.purchasing import (
    build_overdue_shipments_query,
    build_pending_requisitions_query,
    build_po_import_rejections_query,
    build_po_query,
)
from ebsmcp.tools.scm.receiving import build_receiving_errors_query
from ebsmcp.tools.scm.shipping import build_backordered_lines_query, build_open_deliveries_query

ORGS = ("204", "207")

ALL_QUERIES = [
    build_onhand_query(ORGS, "AS54888", "STORES", "m1"),
    build_item_lookup_query(ORGS, "AS5", "m1"),
    build_transaction_errors_query(ORGS, "m1"),
    build_po_query(ORGS, "1001", "APPROVED", "acme"),
    build_pending_requisitions_query(ORGS, 7),
    build_order_query(ORGS, 5001, "booked", "acme"),
    build_orders_on_hold_query(ORGS, "Credit Check Failure"),
    build_overdue_shipments_query(ORGS, 3),
    build_po_import_rejections_query(ORGS),
    build_order_import_errors_query(ORGS),
    build_receiving_errors_query(ORGS, "m1"),
    build_open_deliveries_query(ORGS, "m1"),
    build_backordered_lines_query(ORGS, "m1"),
]


@pytest.mark.parametrize("sql,binds", ALL_QUERIES)
def test_every_query_is_org_scoped_capped_and_conventional(sql, binds):
    validate_sql_conventions(sql)
    assert ":scm_org_0" in sql and ":scm_org_1" in sql
    assert binds["scm_org_0"] == 204 and binds["scm_org_1"] == 207
    assert "FETCH FIRST 50 ROWS ONLY" in sql
    assert "COUNT(*) OVER () AS total_count" in sql
    assert "DISTINCT" not in sql
    assert "APPLSYS." not in sql


@pytest.mark.parametrize("sql,binds", ALL_QUERIES)
def test_no_caller_value_reaches_sql_text(sql, binds):
    for value in binds.values():
        if isinstance(value, str):
            assert value.strip("%") not in sql


def test_org_in_clause_rejects_empty_and_non_numeric():
    with pytest.raises(Exception, match="No effective Org IDs"):
        org_in_clause("x.org_id", ())
    with pytest.raises(Exception, match="not numeric"):
        org_in_clause("x.org_id", ("204; DROP TABLE",))


def test_like_wildcards_are_escaped():
    assert escape_like("50%_off\\") == "50\\%\\_off\\\\"
    _, binds = build_item_lookup_query(ORGS, "A%_", None)
    assert binds["item_prefix"] == "A\\%\\_%"


def test_onhand_scopes_through_operating_unit():
    sql, _ = build_onhand_query(ORGS, None, None, None)
    assert "ood.operating_unit IN" in sql
    assert "item_number" not in sql.split("WHERE")[1]


def test_optional_filters_only_appear_when_given():
    sql, binds = build_po_query(ORGS, None, None, None)
    assert set(binds) == {"scm_org_0", "scm_org_1"}
    sql, binds = build_po_query(ORGS, "1001", None, None)
    assert binds["po_number"] == "1001" and "poh.segment1 = :po_number" in sql


def test_transaction_errors_only_reads_error_rows():
    sql, _ = build_transaction_errors_query(ORGS, None)
    assert "mti.process_flag = 3" in sql


def test_overdue_shipments_only_reads_open_quantity():
    sql, _ = build_overdue_shipments_query(ORGS, None)
    assert "NVL(pll.cancel_flag, 'N') = 'N'" in sql
    assert "quantity_received" in sql and "pll.need_by_date < TRUNC(SYSDATE)" in sql


def test_holds_exclude_released_and_line_level():
    sql, _ = build_orders_on_hold_query(ORGS, None)
    assert "oh.released_flag = 'N'" in sql
    assert "oh.line_id IS NULL" in sql


class RecordingConnector(EBSConnector):
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute_query(self, sql, binds=None):
        self.calls.append((sql, binds))
        return self.rows


async def call(app, name, args):
    async with InMemoryTransport(app)._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)


def build_app(connector, allowed=("204", "207")):
    app = MCPServer("scm-test")
    ctx = ToolContext(
        connectors={"TEST": connector},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject="u@corp.com", environment="test", target_system="ebs",
                mapped_role="SCM_USER", allowed_org_ids=allowed,
            )
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject="u@corp.com",
    )
    mount_toolsets(app, ctx, [
            INVENTORY_TOOLSET,
            PURCHASING_TOOLSET,
            ORDER_MANAGEMENT_TOOLSET,
            RECEIVING_TOOLSET,
            SHIPPING_TOOLSET,
        ])
    return app


@pytest.mark.asyncio
async def test_tool_lifts_total_count_and_scopes_to_caller_orgs():
    conn = RecordingConnector(
        [{"po_number": "1001", "total_count": 120}, {"po_number": "1002", "total_count": 120}]
    )
    result = await call(build_app(conn), "purchase_order_status", {})
    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["total_count"] == 120
    assert body["row_cap"] == 50
    assert all("total_count" not in r for r in body["purchase_orders"])
    assert body["effective_org_ids"] == ["204", "207"]


@pytest.mark.asyncio
async def test_requested_orgs_narrow_but_never_widen():
    conn = RecordingConnector([])
    result = await call(build_app(conn), "orders_on_hold", {"org_ids": ["207", "999"]})
    assert not result.is_error
    _, binds = conn.calls[0]
    assert binds["scm_org_0"] == 207 and "scm_org_1" not in binds

    denied = await call(build_app(RecordingConnector([])), "orders_on_hold", {"org_ids": ["999"]})
    assert denied.is_error
    assert "no permitted Org ID overlap" in denied.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args",
    [
        ("onhand_quantity", {}),
        ("item_lookup", {"item_prefix": "AS"}),
        ("inventory_transaction_errors", {}),
        ("purchase_order_status", {}),
        ("requisitions_pending_approval", {}),
        ("sales_order_status", {}),
        ("orders_on_hold", {}),
        ("overdue_po_shipments", {}),
        ("po_import_rejections", {}),
        ("order_import_errors", {}),
        ("receiving_interface_errors", {}),
        ("open_deliveries", {}),
        ("backordered_lines", {}),
    ],
)
async def test_every_tool_runs_end_to_end(tool, args):
    conn = RecordingConnector([])
    result = await call(build_app(conn), tool, args)
    assert not result.is_error
    assert len(conn.calls) == 1
