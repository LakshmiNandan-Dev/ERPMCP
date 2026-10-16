"""Quality: lot genealogy (forward and backward trace for recalls).

Not covered yet: quality_results and nonconformance_summary. Quality
collection plans are user-defined — each plan maps its elements onto
generic CHARn columns of QA_RESULTS through QA_PLAN_CHARS — so there is no
fixed column set to write reviewed SQL against without introspecting a real
instance's plans. They should be added once a live instance can be queried.

Scoping: lots are scoped through MTL_LOT_NUMBERS.ORGANIZATION_ID ->
APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT, applied to the starting lot
and to every lot returned. The trace walks through intermediate WIP jobs
(a lot is consumed into a job, which produces the next lot) without showing
them, and never shows a lot outside the caller's operating units.

Confidence: written from the documented R12 data model, then executed against
a live R12.2 instance on 2026-10-15, which corrected it: the graph is
lot -> job -> lot, not lot -> lot, and the first version returned nothing for
any lot that had been consumed into a job. After the fix, forward from lot
LPU2500 reached CP8200 and backward from CP8200 reached LPU2500 and LPU8200,
two hops each (the job in between). MTL_LOT_NUMBERS.GEN_OBJECT_ID links a lot
to MTL_OBJECT_GENEALOGY.OBJECT_ID / PARENT_OBJECT_ID. On that instance a
lot -> job edge carried OBJECT_TYPE 5 and a job -> lot edge OBJECT_TYPE 1, so
OBJECT_TYPE is not a node-type column and nothing here depends on it. Serial
genealogy is deliberately not traced — only lots are returned.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet
from ebsmcp.tools.scoping import MANUFACTURING_DOMAINS, ROW_CAP, inventory_org_filter, run_scoped_list

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
    """Pure function, unit-tested directly. Every lot reached from (forward)
    or leading to (backward) the starting lot, with the number of genealogy
    hops from it.

    The traversal runs over MTL_OBJECT_GENEALOGY with no joins, because the
    graph is lot -> job -> lot: verified on a live instance (2026-10-15) that
    a lot's children are WIP jobs (OBJECT_TYPE 5), which in turn produce the
    next lots. Joining to MTL_LOT_NUMBERS inside the hierarchy — the first
    version — dropped the job nodes and so cut every trace at its first hop.
    Lots are joined, and scoped to the caller's operating units, only in the
    outer query: intermediate jobs are walked but never shown, and a lot in
    an operating unit the caller cannot see is neither shown nor named as
    anyone's parent."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    scope = " AND ".join(where)
    binds["lot_number"] = lot_number
    binds["max_levels"] = max(1, min(max_levels, MAX_LEVELS_LIMIT))

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
        f"{start_item_join}WHERE st.lot_number = :lot_number AND {scope})"
    )
    if direction == "forward":
        reached = "mog.object_id"
        start = f"START WITH mog.parent_object_id IN {start_lots} "
        connect = "CONNECT BY NOCYCLE PRIOR mog.object_id = mog.parent_object_id "
    else:
        reached = "mog.parent_object_id"
        start = f"START WITH mog.object_id IN {start_lots} "
        connect = "CONNECT BY NOCYCLE PRIOR mog.parent_object_id = mog.object_id "

    inner = (
        f"SELECT LEVEL AS hops, {reached} AS reached_object_id, "
        "SYS_CONNECT_BY_PATH(TO_CHAR(mog.object_id), '>') AS sort_path "
        "FROM APPS.MTL_OBJECT_GENEALOGY mog "
        f"{start}{connect}"
        "AND LEVEL <= :max_levels"
    )
    # Outer query: one row per lot (the same lot is reached once per edge and
    # per path — verified live, a lot made by one job appeared twice) at its
    # nearest hop count, in connect-by order. GROUP BY, not DISTINCT, so the
    # COUNT(*) OVER () total counts lots; ORDER SIBLINGS BY cannot share a
    # query with that total (ORA-30929, verified live).
    sql = (
        "SELECT MIN(t.hops) AS hops, mln.lot_number, msi.segment1 AS item_number, "
        "ood.organization_code, COUNT(*) OVER () AS total_count "
        f"FROM ({inner}) t "
        "JOIN APPS.MTL_LOT_NUMBERS mln ON mln.gen_object_id = t.reached_object_id "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B msi ON msi.inventory_item_id = mln.inventory_item_id "
        "AND msi.organization_id = mln.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        f"ON ood.organization_id = mln.organization_id AND {scope} "
        "GROUP BY mln.lot_number, msi.segment1, ood.organization_code "
        "ORDER BY MIN(t.hops), mln.lot_number "
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
        lists the lots it was made from (where did it come from). Lots are
        linked through the WIP jobs that consumed and produced them; the
        jobs are walked but not listed. Each row is a lot reached, with its
        item, organization and hops (genealogy steps from the starting lot,
        counting the jobs in between, so a lot made in one job is 2 hops
        away). Pass item_number when the lot number is reused across items.
        Lots only; serials are not traced. max_levels counts hops and is
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
            key="edges", domains=MANUFACTURING_DOMAINS, row_cap=ROW_CAP * 4,
        )


QUALITY_TOOLSET = ToolSet(name="mfg_quality", register=_register)
