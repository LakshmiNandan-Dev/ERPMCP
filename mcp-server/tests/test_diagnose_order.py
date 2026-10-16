"""diagnose_order: query construction, the pure findings logic, and the tool
end to end over the in-memory MCP transport."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import DIAGNOSE_ORDER_TOOLSET, ToolContext, mount_toolsets
from ebsmcp.tools.scm.diagnose_order import (
    build_order_header_query,
    build_order_holds_query,
    build_order_lines_query,
    build_order_shipping_query,
    build_order_workflow_errors_query,
    diagnose_order_findings,
)

NOW = datetime(2026, 10, 16)
ORGS = ("204", "207")


# ── queries ──────────────────────────────────────────────────────────────────


def test_header_query_is_org_scoped_and_binds_the_number():
    sql, binds = build_order_header_query(ORGS, 12345)
    validate_sql_conventions(sql)
    assert "ooh.org_id IN (:scm_org_0, :scm_org_1)" in sql
    assert binds == {"scm_org_0": 204, "scm_org_1": 207, "order_number": 12345}
    assert "12345" not in sql
    assert "FETCH FIRST 5 ROWS ONLY" in sql  # enough to detect a repeated number


@pytest.mark.parametrize(
    "built",
    [
        build_order_lines_query(11, 204),
        build_order_holds_query(11, 204),
        build_order_shipping_query(11),
        build_order_workflow_errors_query(11),
    ],
)
def test_follow_up_queries_are_keyed_by_ids_only_and_capped(built):
    sql, binds = built
    validate_sql_conventions(sql)
    assert "FETCH FIRST" in sql
    assert set(binds) <= {"header_id", "org_id"}
    assert set(re.findall(r":([a-z_]+)", sql)) <= set(binds)
    assert "APPLSYS." not in sql


def test_lines_exclude_cancelled_and_carry_warehouse_onhand():
    sql, _ = build_order_lines_query(11, 204)
    assert "NVL(oel.cancelled_flag, 'N') <> 'Y'" in sql
    assert "moq.organization_id = oel.ship_from_org_id" in sql


def test_holds_include_line_level_and_exclude_released():
    sql, _ = build_order_holds_query(11, 204)
    assert "oh.released_flag = 'N'" in sql
    assert "oh.line_id IS NULL" not in sql  # line-level holds must be reported too


def test_workflow_query_covers_header_and_line_items():
    sql, _ = build_order_workflow_errors_query(11)
    assert "'OEOH'" in sql and "'OEOL'" in sql
    assert "wias.activity_status = 'ERROR'" in sql


# ── findings ─────────────────────────────────────────────────────────────────


def header(**kw):
    return {"flow_status_code": "BOOKED", "booked_flag": "Y", "cancelled_flag": "N", **kw}


def line(n=1, **kw):
    base = {
        "line_id": 100 + n, "line_number": n, "shipment_number": 1, "open_flag": "Y",
        "flow_status_code": "AWAITING_SHIPPING", "ordered_quantity": 10, "shipped_quantity": 0,
        "schedule_ship_date": datetime(2026, 10, 20), "promise_date": datetime(2026, 10, 10),
        "request_date": datetime(2026, 10, 5), "actual_shipment_date": None,
        "onhand_quantity": 100,
    }
    base.update(kw)
    return base


def ship(line_id, status, **kw):
    return {"line_id": line_id, "released_status": status, **kw}


def codes(out):
    return [f["code"] for f in out["findings"]]


def test_late_line_with_no_cause_reports_lateness_and_no_findings():
    out = diagnose_order_findings(
        header(), [line(1)], [], [ship(101, "C")], [], NOW
    )
    assert out["late"] is True and out["late_lines"] == 1 and out["max_days_late"] == 6
    assert out["findings"] == [] and out["blocked"] is False
    assert out["lines"][0]["days_late"] == 6


def test_each_distinct_hold_is_reported_not_swallowed_by_the_first():
    out = diagnose_order_findings(
        header(), [line(1), line(2)],
        [{"line_id": None, "hold_name": "Credit Check", "days_on_hold": 9},
         {"line_id": 102, "hold_name": "Export Review", "days_on_hold": 2}],
        [], [], NOW,
    )
    holds = [f for f in out["findings"] if f["code"] == "active_hold"]
    assert len(holds) == 2 and all(f["severity"] == "blocking" for f in holds)
    by_name = {("Credit Check" if "Credit" in f["message"] else "Export Review"): f for f in holds}
    assert by_name["Credit Check"]["lines"] == []          # order level
    assert by_name["Export Review"]["lines"] == ["2.1"]     # line level
    assert out["blocked"] is True


def test_same_hold_on_several_lines_groups_into_one_finding():
    out = diagnose_order_findings(
        header(), [line(1), line(2)],
        [{"line_id": 101, "hold_name": "Credit Check", "days_on_hold": 3},
         {"line_id": 102, "hold_name": "Credit Check", "days_on_hold": 3}],
        [], [], NOW,
    )
    (hold,) = [f for f in out["findings"] if f["code"] == "active_hold"]
    assert hold["lines"] == ["1.1", "2.1"]


def test_distinct_workflow_errors_are_each_reported():
    out = diagnose_order_findings(
        header(), [line(1)], [], [ship(101, "C")],
        [{"item_type": "OEOL", "error_message": "Invalid warehouse"},
         {"item_type": "OEOH", "error_message": "No price list"}], NOW,
    )
    messages = " ".join(f["message"] for f in out["findings"] if f["code"] == "workflow_error")
    assert "Invalid warehouse" in messages and "No price list" in messages


def test_not_booked_blocks():
    out = diagnose_order_findings(header(booked_flag="N"), [line(1)], [], [], [], NOW)
    assert codes(out)[0] == "not_booked"


def test_closed_order_is_informational_only():
    out = diagnose_order_findings(
        header(flow_status_code="CLOSED"), [line(1, open_flag="N", shipped_quantity=10)],
        [], [], [], NOW,
    )
    assert [f["severity"] for f in out["findings"]] == ["info"]
    assert out["late"] is False


def test_unscheduled_line_blocks_and_groups_line_numbers():
    out = diagnose_order_findings(
        header(), [line(1, schedule_ship_date=None), line(2, schedule_ship_date=None)],
        [], [], [], NOW,
    )
    f = out["findings"][0]
    assert f["code"] == "unscheduled" and f["severity"] == "blocking"
    assert f["lines"] == ["1.1", "2.1"]


def test_shipping_states_map_to_distinct_causes():
    lines = [line(i) for i in range(1, 6)]
    shipping = [ship(101, "B"), ship(102, "R"), ship(103, "S"), ship(104, "Y"), ship(105, "N")]
    out = diagnose_order_findings(header(), lines, [], shipping, [], NOW)
    assert set(codes(out)) >= {
        "backordered", "awaiting_pick_release", "awaiting_pick_confirm",
        "awaiting_ship_confirm", "shipping_not_ready",
    }
    assert out["findings"][0]["code"] == "backordered"  # blocking sorts first


def test_awaiting_shipping_without_a_delivery_line_is_flagged():
    out = diagnose_order_findings(header(), [line(1)], [], [], [], NOW)
    assert "no_delivery_line" in codes(out)


def test_low_onhand_flagged_unless_picking_has_started():
    low = line(1, onhand_quantity=3)
    assert "insufficient_onhand" in codes(
        diagnose_order_findings(header(), [low], [], [ship(101, "R")], [], NOW)
    )
    assert "insufficient_onhand" not in codes(
        diagnose_order_findings(header(), [low], [], [ship(101, "S")], [], NOW)
    )


def test_workflow_error_text_is_reported():
    out = diagnose_order_findings(
        header(), [line(1)], [], [ship(101, "C")],
        [{"item_type": "OEOL", "error_message": "Invalid warehouse"}], NOW,
    )
    f = [x for x in out["findings"] if x["code"] == "workflow_error"][0]
    assert "Invalid warehouse" in f["message"] and f["severity"] == "blocking"


def test_shipped_late_is_informational():
    shipped = line(1, open_flag="N", shipped_quantity=10,
                   actual_shipment_date=datetime(2026, 10, 14))
    out = diagnose_order_findings(header(), [shipped], [], [], [], NOW)
    assert codes(out) == ["shipped_late"] and out["late"] is False


def test_not_yet_due_line_is_not_late():
    out = diagnose_order_findings(
        header(), [line(1, promise_date=datetime(2026, 10, 30))], [], [ship(101, "C")], [], NOW
    )
    assert out["late"] is False and out["lines"][0]["days_late"] is None


def test_long_line_lists_are_truncated_but_counted():
    lines = [line(i, schedule_ship_date=None) for i in range(1, 16)]
    f = diagnose_order_findings(header(), lines, [], [], [], NOW)["findings"][0]
    assert len(f["lines"]) == 10 and f["more_lines"] == 5


# ── end to end ───────────────────────────────────────────────────────────────


class Routing(EBSConnector):
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def execute_query(self, sql, binds=None):
        self.calls.append((sql, binds))
        for key, rows in self.responses.items():
            if key in sql:
                return rows
        return []


async def call(app, args):
    async with InMemoryTransport(app)._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("diagnose_order", args)


def build_app(connector, allowed=("204",), domain="scm"):
    app = MCPServer("t")
    ctx = ToolContext(
        connectors={"T": connector},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject="u@corp.com", environment="test", target_system="ebs",
                mapped_role="OM", allowed_org_ids=allowed, domain=domain,
            )
        ),
        entitlement=EntitlementFilter(), audit=AuditLogger(),
        environment="test", dev_subject="u@corp.com",
    )
    mount_toolsets(app, ctx, [DIAGNOSE_ORDER_TOOLSET])
    return app


HEADER = {"header_id": 11, "org_id": 204, "order_number": 5001, "flow_status_code": "BOOKED",
          "booked_flag": "Y", "cancelled_flag": "N", "customer_name": "Acme"}


@pytest.mark.asyncio
async def test_composes_all_checks_from_the_scoped_header():
    conn = Routing({
        "FROM APPS.OE_ORDER_HEADERS_ALL ooh": [HEADER],
        "FROM APPS.OE_ORDER_LINES_ALL oel WHERE oel.header_id": [line(1, schedule_ship_date=None)],
        "OE_ORDER_HOLDS_ALL": [{"line_id": None, "hold_name": "Credit Check", "days_on_hold": 4}],
    })
    result = await call(build_app(conn), {"order_number": 5001})
    assert not result.is_error, result.content[0].text
    body = json.loads(result.content[0].text)
    assert body["blocked"] is True
    assert {"unscheduled", "active_hold"} <= {f["code"] for f in body["findings"]}
    assert body["order"]["customer_name"] == "Acme"
    assert len(conn.calls) == 5
    # follow-ups are keyed by ids from the scoped header, never by caller input
    assert conn.calls[1][1] == {"header_id": 11, "org_id": 204}
    assert conn.calls[3][1] == {"header_id": 11}


@pytest.mark.asyncio
async def test_unknown_order_is_an_error_not_an_empty_diagnosis():
    result = await call(build_app(Routing({})), {"order_number": 1})
    assert result.is_error and "No sales order 1" in result.content[0].text


@pytest.mark.asyncio
async def test_order_number_reused_across_operating_units_is_not_guessed():
    other = {**HEADER, "header_id": 12, "org_id": 207}
    conn = Routing({"FROM APPS.OE_ORDER_HEADERS_ALL ooh": [HEADER, other]})
    result = await call(build_app(conn, allowed=("204", "207")), {"order_number": 5001})
    assert result.is_error and "more than one operating unit" in result.content[0].text
    assert len(conn.calls) == 1  # stopped before any follow-up query


@pytest.mark.asyncio
async def test_org_ids_narrow_to_one_operating_unit():
    conn = Routing({"FROM APPS.OE_ORDER_HEADERS_ALL ooh": [HEADER]})
    result = await call(build_app(conn, allowed=("204", "207")),
                        {"order_number": 5001, "org_ids": ["204"]})
    assert not result.is_error
    assert conn.calls[0][1]["scm_org_0"] == 204 and "scm_org_1" not in conn.calls[0][1]


@pytest.mark.asyncio
async def test_caller_without_a_scm_mapping_is_denied():
    result = await call(build_app(Routing({}), domain="hcm"), {"order_number": 5001})
    assert result.is_error and "No identity mapping" in result.content[0].text
