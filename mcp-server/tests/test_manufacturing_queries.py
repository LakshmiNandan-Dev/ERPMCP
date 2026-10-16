"""Unit tests of the manufacturing build_* query functions and pure
annotate/summarize/diagnose functions, plus every tool end to end over the
in-memory MCP transport. Shared properties (org scoping, bound values, SQL
conventions) are asserted for every query in one place.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import (
    COSTING_TOOLSET,
    DIAGNOSE_JOB_TOOLSET,
    EAM_TOOLSET,
    ENGINEERING_TOOLSET,
    PERIOD_CLOSE_TOOLSET,
    QUALITY_TOOLSET,
    WIP_TOOLSET,
    ToolContext,
    mount_toolsets,
)
from ebsmcp.tools.manufacturing.costing import (
    build_item_cost_query,
    build_job_variance_query,
    build_uncosted_query,
    build_wip_value_query,
    summarize_job_variance,
    WPB_ELEMENTS,
)
from ebsmcp.tools.manufacturing.diagnose_job import (
    build_job_header_query,
    build_job_uncosted_query,
    diagnose_job_findings,
)
from ebsmcp.tools.manufacturing.eam import build_asset_work_orders_query
from ebsmcp.tools.manufacturing.engineering import (
    build_bom_explosion_query,
    build_integrity_query,
    build_pending_ecos_query,
    build_routing_detail_query,
    build_where_used_query,
)
from ebsmcp.tools.manufacturing.period_close import (
    annotate_period_close,
    build_period_close_query,
)
from ebsmcp.tools.manufacturing.quality import build_lot_genealogy_query
from ebsmcp.tools.manufacturing.wip import (
    annotate_job_status,
    build_job_progress_query,
    build_job_status_query,
    build_material_shortages_query,
    build_pending_transactions_query,
)

ORGS = ("204", "207")

SCOPED_QUERIES = {
    "job_status": build_job_status_query(ORGS, "m1", 7, False),
    "pending_move": build_pending_transactions_query(ORGS, "m1", "move", True),
    "pending_resource": build_pending_transactions_query(ORGS, "m1", "resource", False, "J1"),
    "shortages": build_material_shortages_query(ORGS, "m1", "J1"),
    "progress": build_job_progress_query(ORGS, "m1", "J1"),
    "bom_explosion": build_bom_explosion_query(ORGS, "m1", "AS54888", 10, False),
    "where_used": build_where_used_query(ORGS, "m1", "CM1", 5, True),
    "routing": build_routing_detail_query(ORGS, "m1", "AS54888"),
    "ecos": build_pending_ecos_query(ORGS, "m1", "AS54888"),
    "no_bill": build_integrity_query(ORGS, "m1", "no_bill"),
    "no_routing": build_integrity_query(ORGS, "m1", "no_routing"),
    "inactive": build_integrity_query(ORGS, "m1", "inactive_components"),
    "loops": build_integrity_query(ORGS, "m1", "loops"),
    "lot_forward": build_lot_genealogy_query(ORGS, "L1", "AS54888", "m1", "forward", 10),
    "lot_backward": build_lot_genealogy_query(ORGS, "L1", None, None, "backward", 10),
    "item_cost": build_item_cost_query(ORGS, "m1", "AS54888", "Frozen"),
    "job_variance": build_job_variance_query(ORGS, "m1", "J1"),
    "uncosted": build_uncosted_query(ORGS, "m1", 90),
    "wip_value": build_wip_value_query(ORGS, "m1"),
    "asset_wo": build_asset_work_orders_query(ORGS, "m1", "A1", "mx", True),
    "period_close": build_period_close_query(ORGS, "m1"),
    "job_header": build_job_header_query(ORGS, "m1", "J1"),
}

# A single-row aggregate has no window total; everything else lists rows.
_NO_WINDOW = {"job_variance", "job_header"}
_NO_CAP = {"job_variance", "job_header"}


@pytest.mark.parametrize("name", sorted(SCOPED_QUERIES))
def test_every_query_is_org_scoped_and_conventional(name):
    sql, binds = SCOPED_QUERIES[name]
    validate_sql_conventions(sql)
    assert ":scm_org_0" in sql and ":scm_org_1" in sql
    assert binds["scm_org_0"] == 204 and binds["scm_org_1"] == 207
    assert "APPLSYS." not in sql
    assert "DISTINCT" not in sql
    if name not in _NO_WINDOW:
        assert "COUNT(*) OVER () AS total_count" in sql
    if name not in _NO_CAP:
        assert "FETCH FIRST" in sql


@pytest.mark.parametrize("name", sorted(SCOPED_QUERIES))
def test_no_caller_value_reaches_sql_text(name):
    sql, binds = SCOPED_QUERIES[name]
    for value in binds.values():
        if isinstance(value, str):
            assert value not in sql


@pytest.mark.parametrize("name", sorted(SCOPED_QUERIES))
def test_every_named_bind_in_sql_has_a_value(name):
    import re

    sql, binds = SCOPED_QUERIES[name]
    used = set(re.findall(r":([a-z_0-9]+)", sql))
    assert used <= set(binds), used - set(binds)


@pytest.mark.parametrize(
    "name", ["bom_explosion", "where_used", "loops"],
)
def test_hierarchy_filters_live_in_join_not_where(name):
    """Oracle applies WHERE after building the hierarchy, so a WHERE filter
    would drop a row but still return its subtree."""
    sql, _ = SCOPED_QUERIES[name]
    before_hierarchy = sql.split(" START WITH ")[0].split(" CONNECT BY ")[0]
    where = before_hierarchy.split(" WHERE ")[1] if " WHERE " in before_hierarchy else ""
    assert "alternate_bom_designator" not in where
    assert "effectivity_date" not in where
    assert "operating_unit" not in where
    assert "bsb.alternate_bom_designator IS NULL" in before_hierarchy
    assert "NOCYCLE" in sql


def test_include_inactive_still_excludes_pending_eco_components():
    sql, _ = build_bom_explosion_query(ORGS, "m1", "AS54888", 10, True)
    assert "bcb.implementation_date IS NOT NULL" in sql
    assert "effectivity_date <= SYSDATE" not in sql


@pytest.mark.parametrize("levels,expected", [(0, 1), (5, 5), (500, 20)])
def test_levels_are_clamped(levels, expected):
    _, binds = build_bom_explosion_query(ORGS, "m1", "X", levels, False)
    assert binds["max_levels"] == expected


def test_forward_and_backward_genealogy_walk_opposite_ways():
    fwd, _ = build_lot_genealogy_query(ORGS, "L1", None, None, "forward", 5)
    bwd, _ = build_lot_genealogy_query(ORGS, "L1", None, None, "backward", 5)
    assert "START WITH mog.parent_object_id IN" in fwd
    assert "PRIOR mog.object_id = mog.parent_object_id" in fwd
    assert "START WITH mog.object_id IN" in bwd
    assert "PRIOR mog.parent_object_id = mog.object_id" in bwd


def test_genealogy_start_lot_is_org_scoped():
    sql, _ = build_lot_genealogy_query(ORGS, "L1", None, None, "forward", 5)
    assert sql.count("operating_unit IN") == 2  # start lot and every returned edge


def test_pending_transactions_error_only_by_default():
    sql, _ = build_pending_transactions_query(ORGS, None, "move", False)
    assert "process_status = 3" in sql
    sql, _ = build_pending_transactions_query(ORGS, None, "move", True)
    assert "process_status IN (1, 2, 3)" in sql


def test_shortage_requires_outstanding_to_exceed_onhand():
    sql, _ = build_material_shortages_query(ORGS, None, None)
    assert sql.count("MTL_ONHAND_QUANTITIES_DETAIL") == 2
    assert "<> 6" in sql  # phantoms excluded


def test_job_uncosted_takes_only_ids():
    sql, binds = build_job_uncosted_query(11, 22)
    validate_sql_conventions(sql)
    assert binds == {"wip_entity_id": 11, "organization_id": 22}


def test_wpb_sums_cover_every_element_and_flow():
    sql, _ = build_job_variance_query(ORGS, None, "J1")
    for element in WPB_ELEMENTS:
        for flow in ("in", "out", "var"):
            assert f"AS {element}_{flow}" in sql


# ── pure functions ───────────────────────────────────────────────────────────


def test_job_status_labels_attached():
    rows = annotate_job_status([{"status_type": 3}, {"status_type": 99}])
    assert rows[0]["status"] == "Released"
    assert "Unknown" in rows[1]["status"]


def test_job_variance_found_false_on_all_null_row():
    nulls = {f"{e}_{f}": None for e in WPB_ELEMENTS for f in ("in", "out", "var")}
    assert summarize_job_variance([nulls])["found"] is False
    assert summarize_job_variance([])["found"] is False


def test_job_variance_totals_are_computed_in_python():
    row = {f"{e}_{f}": 0 for e in WPB_ELEMENTS for f in ("in", "out", "var")}
    row.update(material_in=100, material_out=60, material_var=10, resource_in=20)
    out = summarize_job_variance([row])
    assert out["found"] is True
    assert out["totals"]["in"] == 120
    assert out["totals"]["unrelieved_balance"] == 120 - 60 - 10
    material = next(e for e in out["elements"] if e["cost_element"] == "Material")
    assert material["unrelieved_balance"] == 30


def test_period_close_verdict():
    rows = annotate_period_close(
        [
            {"organization_code": "M1", "earliest_open_period_start": datetime(2026, 9, 1),
             "pending_material_transactions": 0, "material_interface_rows": 3,
             "uncosted_transactions": 0, "pending_wip_move_rows": 0,
             "pending_wip_resource_rows": 0, "pending_receiving_rows": 0},
            {"organization_code": "M2", "earliest_open_period_start": datetime(2026, 9, 1),
             "pending_material_transactions": 0, "material_interface_rows": 0,
             "uncosted_transactions": 0, "pending_wip_move_rows": 0,
             "pending_wip_resource_rows": 0, "pending_receiving_rows": 0},
            {"organization_code": "M3", "earliest_open_period_start": None,
             "pending_material_transactions": 0, "material_interface_rows": 0,
             "uncosted_transactions": 0, "pending_wip_move_rows": 0,
             "pending_wip_resource_rows": 0, "pending_receiving_rows": 0},
        ]
    )
    assert rows[0]["ready_to_close"] is False
    assert [b["check"] for b in rows[0]["blockers"]] == ["material_interface_rows"]
    assert rows[1]["ready_to_close"] is True
    assert rows[2]["no_open_period"] is True and rows[2]["ready_to_close"] is False


NOW = datetime(2026, 10, 10)


def _header(**kw):
    base = {"status_type": 3, "quantity_completed": 0,
            "scheduled_completion_date": datetime(2026, 10, 20)}
    base.update(kw)
    return base


def test_diagnose_unreleased_job_is_blocked():
    out = diagnose_job_findings(_header(status_type=1), [], [], [], [], [], NOW)
    assert out["blocked"] is True
    assert out["status"] == "Unreleased"
    assert out["findings"][0]["code"] == "unreleased"


def test_diagnose_orders_blocking_before_warning_and_reports_error_text():
    out = diagnose_job_findings(
        _header(scheduled_completion_date=datetime(2026, 10, 1)),
        [{"operation_seq_num": 10, "quantity_in_queue": 5, "quantity_running": 0,
          "quantity_waiting_to_move": 0}],
        [{"date_required": datetime(2026, 10, 2)}],
        [{"error_message": "Invalid operation sequence"}],
        [],
        [{"transaction_count": 4}],
        NOW,
    )
    codes = [f["code"] for f in out["findings"]]
    assert codes[:2] == ["material_shortage", "move_transaction_error"]
    assert set(codes[2:]) == {"uncosted_transactions", "past_schedule"}
    assert "Invalid operation sequence" in out["findings"][1]["message"]


def test_diagnose_finds_quantity_waiting_at_last_operation():
    out = diagnose_job_findings(
        _header(),
        [{"operation_seq_num": 10, "quantity_waiting_to_move": 0},
         {"operation_seq_num": 20, "quantity_waiting_to_move": 8}],
        [], [], [], [], NOW,
    )
    assert [f["code"] for f in out["findings"]] == ["awaiting_completion"]
    assert out["blocked"] is False


def test_diagnose_healthy_job_has_no_findings():
    out = diagnose_job_findings(
        _header(), [{"operation_seq_num": 10, "quantity_in_queue": 3}], [], [], [], [], NOW
    )
    assert out["findings"] == [] and out["blocked"] is False


def test_diagnose_closed_job_only_informational():
    out = diagnose_job_findings(_header(status_type=12), [], [], [], [], [], NOW)
    assert [f["severity"] for f in out["findings"]] == ["info"]
    assert out["blocked"] is False


# ── end to end ───────────────────────────────────────────────────────────────


class RoutingConnector(EBSConnector):
    """Returns rows by SQL substring, like MockEBSConnector, but records
    every call so tests can assert on binds."""

    def __init__(self, responses=None):
        self.responses, self.calls = responses or {}, []

    def execute_query(self, sql, binds=None):
        self.calls.append((sql, binds))
        for key, rows in self.responses.items():
            if key in sql:
                return rows
        return []


async def call(app, name, args):
    async with InMemoryTransport(app)._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)


def build_app(connector, allowed=("204", "207")):
    app = MCPServer("mfg-test")
    ctx = ToolContext(
        connectors={"TEST": connector},
        identity_resolver=StubIdentityResolver(
            ResolvedIdentity(
                subject="u@corp.com", environment="test", target_system="ebs",
                mapped_role="MFG_USER", allowed_org_ids=allowed,
            )
        ),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject="u@corp.com",
    )
    mount_toolsets(
        app, ctx,
        [WIP_TOOLSET, ENGINEERING_TOOLSET, QUALITY_TOOLSET, COSTING_TOOLSET,
         EAM_TOOLSET, PERIOD_CLOSE_TOOLSET, DIAGNOSE_JOB_TOOLSET],
    )
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args",
    [
        ("wip_job_status", {}),
        ("wip_pending_transactions", {}),
        ("job_material_shortages", {}),
        ("job_progress", {"job_name": "J1"}),
        ("bom_explosion", {"item_number": "A", "organization_code": "M1"}),
        ("where_used", {"component_number": "C", "organization_code": "M1"}),
        ("routing_detail", {"item_number": "A", "organization_code": "M1"}),
        ("pending_ecos", {}),
        ("bom_integrity_check", {"check": "loops", "organization_code": "M1"}),
        ("lot_genealogy", {"lot_number": "L1"}),
        ("item_cost_breakdown", {"item_number": "A", "organization_code": "M1"}),
        ("job_variance", {"job_name": "J1"}),
        ("uncosted_transactions", {}),
        ("wip_value_summary", {}),
        ("asset_work_orders", {}),
        ("inventory_period_close_check", {}),
    ],
)
async def test_every_tool_runs_end_to_end(tool, args):
    conn = RoutingConnector()
    result = await call(build_app(conn), tool, args)
    assert not result.is_error, result.content[0].text
    assert len(conn.calls) == 1
    _, binds = conn.calls[0]
    assert binds["scm_org_0"] == 204


@pytest.mark.asyncio
async def test_requested_orgs_narrow_but_never_widen():
    conn = RoutingConnector()
    ok = await call(build_app(conn), "wip_job_status", {"org_ids": ["207", "999"]})
    assert not ok.is_error
    assert conn.calls[0][1]["scm_org_0"] == 207 and "scm_org_1" not in conn.calls[0][1]
    denied = await call(build_app(RoutingConnector()), "wip_job_status", {"org_ids": ["999"]})
    assert denied.is_error and "no permitted Org ID overlap" in denied.content[0].text


@pytest.mark.asyncio
async def test_period_close_returns_verdicts_and_summary():
    row = {"organization_code": "M1", "earliest_open_period_start": datetime(2026, 9, 1),
           "pending_material_transactions": 2, "material_interface_rows": 0,
           "uncosted_transactions": 0, "pending_wip_move_rows": 0,
           "pending_wip_resource_rows": 0, "pending_receiving_rows": 0, "total_count": 1}
    conn = RoutingConnector({"ORG_ACCT_PERIODS": [row]})
    body = json.loads((await call(build_app(conn), "inventory_period_close_check", {})).content[0].text)
    assert body["organizations"][0]["ready_to_close"] is False
    assert body["summary"]["organizations_blocked"] == 1
    assert body["total_count"] == 1


@pytest.mark.asyncio
async def test_job_variance_reports_not_found_for_empty_aggregate():
    nulls = {f"{e}_{f}": None for e in WPB_ELEMENTS for f in ("in", "out", "var")}
    conn = RoutingConnector({"WIP_PERIOD_BALANCES": [nulls]})
    body = json.loads((await call(build_app(conn), "job_variance", {"job_name": "J1"})).content[0].text)
    assert body["found"] is False


@pytest.mark.asyncio
async def test_diagnose_job_unknown_job_is_an_error():
    result = await call(
        build_app(RoutingConnector()), "diagnose_job",
        {"job_name": "NOPE", "organization_code": "M1"},
    )
    assert result.is_error and "No job 'NOPE'" in result.content[0].text


@pytest.mark.asyncio
async def test_diagnose_job_composes_all_checks():
    header = {"wip_entity_id": 77, "organization_id": 301, "organization_code": "M1",
              "job_name": "J1", "assembly": "AS54888", "status_type": 3,
              "start_quantity": 10, "quantity_completed": 0, "quantity_scrapped": 0,
              "scheduled_start_date": None, "scheduled_completion_date": None,
              "date_released": None, "date_completed": None}
    conn = RoutingConnector({
        "FROM APPS.WIP_DISCRETE_JOBS wdj JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wdj.wip_entity_id": [header],
        "WIP_REQUIREMENT_OPERATIONS": [{"date_required": None, "total_count": 1}],
        "WIP_MOVE_TXN_INTERFACE": [{"error_message": "bad op", "total_count": 1}],
    })
    result = await call(
        build_app(conn), "diagnose_job", {"job_name": "J1", "organization_code": "M1"}
    )
    assert not result.is_error, result.content[0].text
    body = json.loads(result.content[0].text)
    assert body["blocked"] is True
    assert {f["code"] for f in body["findings"]} >= {"material_shortage", "move_transaction_error"}
    assert "total_count" not in body["shortages"][0]
    # six queries: header, progress, shortages, move, resource, uncosted
    assert len(conn.calls) == 6
    assert conn.calls[-1][1] == {"wip_entity_id": 77, "organization_id": 301}


# ── regressions found by running against a live R12.2 instance (2026-10-15) ──


def test_hierarchical_queries_never_combine_order_siblings_with_a_window():
    """ORA-30929: ORDER SIBLINGS BY is rejected whenever the same query
    carries COUNT(*) OVER (). Ordering is by connect-by path in an outer
    query instead."""
    for name in ("bom_explosion", "where_used", "lot_forward", "lot_backward"):
        sql, _ = SCOPED_QUERIES[name]
        assert "ORDER SIBLINGS BY" not in sql, name
        assert "SYS_CONNECT_BY_PATH" in sql, name
        assert "ORDER BY t.sort_path" in sql or "ORDER BY MIN(t.hops)" in sql, name


def test_genealogy_traverses_without_joining_lots_inside_the_hierarchy():
    """Lots are consumed into jobs, which produce the next lots. A join to
    MTL_LOT_NUMBERS inside the hierarchy drops the job nodes and cuts every
    trace at its first hop."""
    for name in ("lot_forward", "lot_backward"):
        sql, _ = SCOPED_QUERIES[name]
        inner = sql.split(" START WITH ")[0]
        assert "MTL_LOT_NUMBERS" not in inner.split("FROM (")[1], name
        assert "GROUP BY mln.lot_number" in sql, name  # one row per lot


def test_job_progress_uses_the_real_schedule_columns():
    sql, _ = SCOPED_QUERIES["progress"]
    assert "wo.scheduled_completion_date" not in sql
    assert "wo.first_unit_start_date" in sql and "wo.last_unit_completion_date" in sql


def test_period_balance_sql_uses_only_columns_the_table_has():
    from ebsmcp.tools.manufacturing.costing import _WPB_COLUMNS

    for name in ("job_variance", "wip_value"):
        sql, _ = SCOPED_QUERIES[name]
        import re

        used = set(re.findall(r"wpb\.([a-z_]+)", sql))
        used -= {"wip_entity_id", "organization_id"}
        assert used <= _WPB_COLUMNS, used - _WPB_COLUMNS
    # the columns the symmetric guess invented
    for missing in ("tl_material_in", "tl_material_overhead_in"):
        assert missing not in _WPB_COLUMNS


def test_wip_value_groups_by_the_jobs_class_code_not_the_balance_row():
    sql, _ = SCOPED_QUERIES["wip_value"]
    assert "wpb.class_code" not in sql
    assert "wdj.class_code" in sql


def test_job_variance_balance_has_no_float_noise():
    row = {f"{e}_{f}": 0 for e in WPB_ELEMENTS for f in ("in", "out", "var")}
    row.update(resource_in=0.1 + 0.2, resource_out=0.3)  # 0.30000000000000004
    out = summarize_job_variance([row])
    assert out["totals"]["unrelieved_balance"] == 0
