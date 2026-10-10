"""Quality: lot genealogy (forward and backward trace for recalls).

Not covered yet: quality_results and nonconformance_summary. Quality
collection plans are user-defined — each plan maps its elements onto
generic CHARn columns of QA_RESULTS through QA_PLAN_CHARS — so there is no
fixed column set to write reviewed SQL against without introspecting a real
instance's plans. They should be added once a live instance can be queried.

Scoping: lots are scoped through MTL_LOT_NUMBERS.ORGANIZATION_ID ->
APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT. The trace only returns
edges whose lot is in a permitted operating unit, so a lineage that passes
through a lot in an operating unit the caller cannot see stops there rather
than leaking it.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Assumptions
to verify: MTL_LOT_NUMBERS.GEN_OBJECT_ID links a lot to
MTL_OBJECT_GENEALOGY.OBJECT_ID / PARENT_OBJECT_ID. Serial-number genealogy is
deliberately not traced — only lot-to-lot edges.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet
from ebsmcp.tools.scoping import ROW_CAP, inventory_org_filter, run_scoped_list

Direction = Literal["forward", "backward"]
MAX_LEVELS_LIMIT = 20


def build_lot_genealogy_query(
    org_ids: tuple[str, ...],
    lot_number: str,
    item_number: str | None,
    organization_code: str | None,
    direction: Direction,
    max_levels: int,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Each row is one genealogy edge:
    parent_lot_number -> lot_number. forward walks from the lot to what was
    made from it; backward walks to what it was made from."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    scope = " AND ".join(where)
    binds["lot_number"] = lot_number
    binds["max_levels"] = max(1, min(max_levels, MAX_LEVELS_LIMIT))

    start_filter = "st.lot_number = :lot_number"
    start_item_join = ""
    if item_number:
        start_item_join = (
            "JOIN APPS.MTL_SYSTEM_ITEMS_B sti ON sti.inventory_item_id = st.inventory_item_id "
            "AND sti.organization_id = st.organization_id AND sti.segment1 = :item_number "
        )
        binds["item_number"] = item_number

    start_lots = (
        "(SELECT st.gen_object_id FROM APPS.MTL_LOT_NUMBERS st "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = st.organization_id "
        f"{start_item_join}WHERE {start_filter} AND {scope})"
    )
    if direction == "forward":
        start = f"START WITH mog.parent_object_id IN {start_lots} "
        connect = "CONNECT BY NOCYCLE PRIOR mog.object_id = mog.parent_object_id "
    else:
        start = f"START WITH mog.object_id IN {start_lots} "
        connect = "CONNECT BY NOCYCLE PRIOR mog.parent_object_id = mog.object_id "

    sql = (
        "SELECT LEVEL AS genealogy_level, mlp.lot_number AS parent_lot_number, "
        "mln.lot_number, msi.segment1 AS item_number, ood.organization_code, "
        "mog.genealogy_type, mog.start_date_active, mog.end_date_active, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.MTL_OBJECT_GENEALOGY mog "
        "JOIN APPS.MTL_LOT_NUMBERS mln ON mln.gen_object_id = mog.object_id "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B msi ON msi.inventory_item_id = mln.inventory_item_id "
        "AND msi.organization_id = mln.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        f"ON ood.organization_id = mln.organization_id AND {scope} "
        "LEFT JOIN APPS.MTL_LOT_NUMBERS mlp ON mlp.gen_object_id = mog.parent_object_id "
        f"{start}{connect}"
        "AND LEVEL <= :max_levels "
        "ORDER SIBLINGS BY mln.lot_number "
        f"FETCH FIRST {ROW_CAP * 4} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def lot_genealogy(
        lot_number: str,
        direction: Direction = "forward",
        item_number: str | None = None,
        organization_code: str | None = None,
        max_levels: int = 10,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Trace a lot through genealogy for a recall. direction "forward"
        lists the lots made from this lot (where did it go); "backward"
        lists the lots it was made from (where did it come from). Each row
        is one edge, parent_lot_number to lot_number, with item and
        organization. Pass item_number when the lot number is reused across
        items. Lot-to-lot only (serials are not traced). max_levels is
        capped at 20; output capped at 200 rows with total_count. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {
            "lot_number": lot_number, "direction": direction, "item_number": item_number,
            "organization_code": organization_code, "max_levels": max_levels, "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="lot_genealogy", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_lot_genealogy_query(
                eff, lot_number, item_number, organization_code, direction, max_levels
            ),
            key="edges", row_cap=ROW_CAP * 4,
        )


QUALITY_TOOLSET = ToolSet(name="mfg_quality", register=_register)
