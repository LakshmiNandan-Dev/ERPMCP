"""Engineering data: BOM explosion, where-used, routings, pending ECOs and a
BOM/routing integrity check.

Scoping: bills and routings carry the inventory ORGANIZATION_ID, scoped
through APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT. Item numbers are
per-organization, so the structure tools require organization_code.

Hierarchical queries put every filter (org scope, primary bill,
effectivity, implemented-only) in the JOIN ... ON clause, not WHERE: Oracle
applies WHERE after the hierarchy is built, so a WHERE filter would prune a
disabled component's row but still return its whole subtree.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Assumptions
to verify: BOM_COMPONENTS_B.IMPLEMENTATION_DATE IS NULL marks a component
pending an ECO; a NULL ALTERNATE_BOM_DESIGNATOR is the primary bill;
BOM_BILL_OF_MATERIALS item type 1 = standard; ENG_ENGINEERING_CHANGES
.STATUS_TYPE 5 = implemented and 6 = cancelled (instance status sets can
add codes); the integrity "loops" check walks every primary bill in one
organization to 20 levels and may be slow on a large item master.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet
from ebsmcp.tools.scoping import ROW_CAP, inventory_org_filter, run_scoped_list

MAX_LEVELS_LIMIT = 20

IntegrityCheck = Literal["no_bill", "no_routing", "inactive_components", "loops"]

_ORG_JOIN_ON = (
    "ON ood.organization_id = bsb.organization_id "
    "AND bsb.alternate_bom_designator IS NULL AND {where} "
)

_EFFECTIVE = (
    "AND bcb.implementation_date IS NOT NULL "
    "AND bcb.effectivity_date <= SYSDATE "
    "AND (bcb.disable_date IS NULL OR bcb.disable_date > SYSDATE) "
)
_ANY_EFFECTIVITY = "AND bcb.implementation_date IS NOT NULL "


def _levels(max_levels: int) -> int:
    return max(1, min(max_levels, MAX_LEVELS_LIMIT))


def build_bom_explosion_query(
    org_ids: tuple[str, ...],
    organization_code: str,
    item_number: str,
    max_levels: int,
    include_inactive: bool,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Multi-level primary bill,
    depth-first, with effectivity dates."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    binds["item_number"] = item_number
    binds["max_levels"] = _levels(max_levels)
    effectivity = _ANY_EFFECTIVITY if include_inactive else _EFFECTIVE

    sql = (
        "SELECT LEVEL AS bom_level, asm.segment1 AS assembly, comp.segment1 AS component, "
        "comp.description AS component_description, bcb.item_num, "
        "bcb.component_quantity, bcb.operation_seq_num, "
        "bcb.effectivity_date, bcb.disable_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.BOM_STRUCTURES_B bsb "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        + _ORG_JOIN_ON.format(where=" AND ".join(where))
        + "JOIN APPS.BOM_COMPONENTS_B bcb ON bcb.bill_sequence_id = bsb.bill_sequence_id "
        f"{effectivity}"
        "JOIN APPS.MTL_SYSTEM_ITEMS_B asm ON asm.inventory_item_id = bsb.assembly_item_id "
        "AND asm.organization_id = bsb.organization_id "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B comp ON comp.inventory_item_id = bcb.component_item_id "
        "AND comp.organization_id = bsb.organization_id "
        "START WITH asm.segment1 = :item_number "
        "CONNECT BY NOCYCLE PRIOR bcb.component_item_id = bsb.assembly_item_id "
        "AND LEVEL <= :max_levels "
        "ORDER SIBLINGS BY bcb.item_num "
        f"FETCH FIRST {ROW_CAP * 4} ROWS ONLY"
    )
    return sql, binds


def build_where_used_query(
    org_ids: tuple[str, ...],
    organization_code: str,
    component_number: str,
    max_levels: int,
    include_inactive: bool,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Assemblies using a component,
    walking upward through parent assemblies."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    binds["component_number"] = component_number
    binds["max_levels"] = _levels(max_levels)
    effectivity = _ANY_EFFECTIVITY if include_inactive else _EFFECTIVE

    sql = (
        "SELECT LEVEL AS where_used_level, asm.segment1 AS assembly, "
        "asm.description AS assembly_description, comp.segment1 AS component, "
        "bcb.component_quantity, bcb.effectivity_date, bcb.disable_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.BOM_STRUCTURES_B bsb "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        + _ORG_JOIN_ON.format(where=" AND ".join(where))
        + "JOIN APPS.BOM_COMPONENTS_B bcb ON bcb.bill_sequence_id = bsb.bill_sequence_id "
        f"{effectivity}"
        "JOIN APPS.MTL_SYSTEM_ITEMS_B asm ON asm.inventory_item_id = bsb.assembly_item_id "
        "AND asm.organization_id = bsb.organization_id "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B comp ON comp.inventory_item_id = bcb.component_item_id "
        "AND comp.organization_id = bsb.organization_id "
        "START WITH comp.segment1 = :component_number "
        "CONNECT BY NOCYCLE PRIOR bsb.assembly_item_id = bcb.component_item_id "
        "AND LEVEL <= :max_levels "
        "ORDER SIBLINGS BY asm.segment1 "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_routing_detail_query(
    org_ids: tuple[str, ...],
    organization_code: str,
    item_number: str,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Primary routing operations with
    each operation's resources and usage rates."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("msi.segment1 = :item_number")
    where.append("bor.alternate_routing_designator IS NULL")
    binds["item_number"] = item_number

    sql = (
        "SELECT bos.operation_seq_num, bd.department_code, bos.operation_description, "
        "br.resource_code, bor2.resource_seq_num, bor2.usage_rate_or_amount, "
        "bor2.basis_type, bor2.assigned_units, br.unit_of_measure, "
        "bos.effectivity_date, bos.disable_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.BOM_OPERATIONAL_ROUTINGS bor "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B msi ON msi.inventory_item_id = bor.assembly_item_id "
        "AND msi.organization_id = bor.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = bor.organization_id "
        "JOIN APPS.BOM_OPERATION_SEQUENCES bos "
        "ON bos.routing_sequence_id = bor.routing_sequence_id "
        "LEFT JOIN APPS.BOM_DEPARTMENTS bd ON bd.department_id = bos.department_id "
        "LEFT JOIN APPS.BOM_OPERATION_RESOURCES bor2 "
        "ON bor2.operation_sequence_id = bos.operation_sequence_id "
        "LEFT JOIN APPS.BOM_RESOURCES br ON br.resource_id = bor2.resource_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY bos.operation_seq_num, bor2.resource_seq_num "
        f"FETCH FIRST {ROW_CAP * 2} ROWS ONLY"
    )
    return sql, binds


def build_pending_ecos_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    item_number: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Engineering change orders not
    yet implemented or cancelled, with the items they revise, oldest
    first."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("eec.status_type NOT IN (5, 6)")
    if item_number:
        where.append("msi.segment1 = :item_number")
        binds["item_number"] = item_number

    sql = (
        "SELECT eec.change_notice, ood.organization_code, eec.status_type, "
        "eec.description, eec.creation_date, msi.segment1 AS revised_item, "
        "eri.scheduled_date, "
        "TRUNC(SYSDATE) - TRUNC(eec.creation_date) AS days_open, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.ENG_ENGINEERING_CHANGES eec "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
        "ON ood.organization_id = eec.organization_id "
        "LEFT JOIN APPS.ENG_REVISED_ITEMS eri ON eri.change_id = eec.change_id "
        "AND eri.organization_id = eec.organization_id "
        "LEFT JOIN APPS.MTL_SYSTEM_ITEMS_B msi "
        "ON msi.inventory_item_id = eri.revised_item_id "
        "AND msi.organization_id = eec.organization_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY eec.creation_date "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def build_integrity_query(
    org_ids: tuple[str, ...],
    organization_code: str,
    check: IntegrityCheck,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One query per check — each
    answers a different question, so they are not unioned."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    scope = " AND ".join(where)

    if check == "no_bill":
        sql = (
            "SELECT ood.organization_code, msi.segment1 AS item_number, msi.description, "
            "COUNT(*) OVER () AS total_count "
            "FROM APPS.MTL_SYSTEM_ITEMS_B msi "
            "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
            "ON ood.organization_id = msi.organization_id "
            f"WHERE {scope} AND msi.planning_make_buy_code = 1 "
            "AND msi.bom_enabled_flag = 'Y' AND msi.bom_item_type = 1 "
            "AND NOT EXISTS (SELECT 1 FROM APPS.BOM_STRUCTURES_B bsb "
            "WHERE bsb.assembly_item_id = msi.inventory_item_id "
            "AND bsb.organization_id = msi.organization_id "
            "AND bsb.alternate_bom_designator IS NULL) "
            "ORDER BY msi.segment1 "
            f"FETCH FIRST {ROW_CAP} ROWS ONLY"
        )
    elif check == "no_routing":
        sql = (
            "SELECT ood.organization_code, msi.segment1 AS item_number, msi.description, "
            "COUNT(*) OVER () AS total_count "
            "FROM APPS.MTL_SYSTEM_ITEMS_B msi "
            "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
            "ON ood.organization_id = msi.organization_id "
            f"WHERE {scope} AND msi.planning_make_buy_code = 1 "
            "AND EXISTS (SELECT 1 FROM APPS.BOM_STRUCTURES_B bsb "
            "WHERE bsb.assembly_item_id = msi.inventory_item_id "
            "AND bsb.organization_id = msi.organization_id "
            "AND bsb.alternate_bom_designator IS NULL) "
            "AND NOT EXISTS (SELECT 1 FROM APPS.BOM_OPERATIONAL_ROUTINGS bor "
            "WHERE bor.assembly_item_id = msi.inventory_item_id "
            "AND bor.organization_id = msi.organization_id "
            "AND bor.alternate_routing_designator IS NULL) "
            "ORDER BY msi.segment1 "
            f"FETCH FIRST {ROW_CAP} ROWS ONLY"
        )
    elif check == "inactive_components":
        sql = (
            "SELECT ood.organization_code, asm.segment1 AS assembly, "
            "comp.segment1 AS component, comp.inventory_item_status_code, "
            "COUNT(*) OVER () AS total_count "
            "FROM APPS.BOM_STRUCTURES_B bsb "
            "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
            "ON ood.organization_id = bsb.organization_id "
            "JOIN APPS.BOM_COMPONENTS_B bcb ON bcb.bill_sequence_id = bsb.bill_sequence_id "
            f"{_EFFECTIVE}"
            "JOIN APPS.MTL_SYSTEM_ITEMS_B asm ON asm.inventory_item_id = bsb.assembly_item_id "
            "AND asm.organization_id = bsb.organization_id "
            "JOIN APPS.MTL_SYSTEM_ITEMS_B comp ON comp.inventory_item_id = bcb.component_item_id "
            "AND comp.organization_id = bsb.organization_id "
            f"WHERE {scope} AND bsb.alternate_bom_designator IS NULL "
            "AND comp.bom_enabled_flag = 'N' "
            "ORDER BY asm.segment1, comp.segment1 "
            f"FETCH FIRST {ROW_CAP} ROWS ONLY"
        )
    else:  # loops
        binds["max_levels"] = MAX_LEVELS_LIMIT
        sql = (
            "SELECT ood.organization_code, asm.segment1 AS assembly, "
            "comp.segment1 AS component, LEVEL AS bom_level, "
            "COUNT(*) OVER () AS total_count "
            "FROM APPS.BOM_STRUCTURES_B bsb "
            "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood "
            "ON ood.organization_id = bsb.organization_id "
            f"AND bsb.alternate_bom_designator IS NULL AND {scope} "
            "JOIN APPS.BOM_COMPONENTS_B bcb ON bcb.bill_sequence_id = bsb.bill_sequence_id "
            f"{_ANY_EFFECTIVITY}"
            "JOIN APPS.MTL_SYSTEM_ITEMS_B asm ON asm.inventory_item_id = bsb.assembly_item_id "
            "AND asm.organization_id = bsb.organization_id "
            "JOIN APPS.MTL_SYSTEM_ITEMS_B comp ON comp.inventory_item_id = bcb.component_item_id "
            "AND comp.organization_id = bsb.organization_id "
            "WHERE CONNECT_BY_ISCYCLE = 1 "
            "CONNECT BY NOCYCLE PRIOR bcb.component_item_id = bsb.assembly_item_id "
            "AND LEVEL <= :max_levels "
            f"FETCH FIRST {ROW_CAP} ROWS ONLY"
        )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def bom_explosion(
        item_number: str,
        organization_code: str,
        max_levels: int = 10,
        include_inactive: bool = False,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Explode the primary bill of materials for an assembly, multi-level
        and depth-first, with each component's quantity, operation sequence
        and effectivity dates. Only currently effective, implemented
        components unless include_inactive (which still excludes components
        pending an ECO). max_levels is capped at 20; output is capped at
        200 rows with total_count. For the reverse question use where_used.
        Call list_ebs_instances first if unsure which EBS instance names
        this deployment has.
        """
        params = {
            "item_number": item_number, "organization_code": organization_code,
            "max_levels": max_levels, "include_inactive": include_inactive, "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="bom_explosion", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_bom_explosion_query(
                eff, organization_code, item_number, max_levels, include_inactive
            ),
            key="components", row_cap=ROW_CAP * 4,
        )

    @app.tool(annotations=read_only)
    def where_used(
        component_number: str,
        organization_code: str,
        max_levels: int = 5,
        include_inactive: bool = False,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List every assembly whose primary bill uses a component, walking
        upward through parent assemblies up to max_levels (cap 20), with
        quantity per assembly and effectivity dates. Capped at 50 rows with
        total_count. For the downward view use bom_explosion. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {
            "component_number": component_number, "organization_code": organization_code,
            "max_levels": max_levels, "include_inactive": include_inactive, "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="where_used", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_where_used_query(
                eff, organization_code, component_number, max_levels, include_inactive
            ),
            key="assemblies",
        )

    @app.tool(annotations=read_only)
    def routing_detail(
        item_number: str,
        organization_code: str,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Show the primary routing for an item: each operation with its
        department, the resources used, usage rate, basis (item or lot) and
        assigned units, in operation order. Capped at 100 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"item_number": item_number, "organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="routing_detail", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_routing_detail_query(eff, organization_code, item_number),
            key="operations", row_cap=ROW_CAP * 2,
        )

    @app.tool(annotations=read_only)
    def pending_ecos(
        item_number: str | None = None,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """List engineering change orders not yet implemented or cancelled,
        oldest first, with the items each revises, scheduled date and days
        open. Optionally for one revised item. Capped at 50 rows with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"item_number": item_number, "organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="pending_ecos", params=params, org_ids=org_ids, instance=instance,
            build=lambda eff: build_pending_ecos_query(eff, organization_code, item_number),
            key="ecos",
        )

    @app.tool(annotations=read_only)
    def bom_integrity_check(
        check: IntegrityCheck,
        organization_code: str,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Find data-quality problems in one organization's bills and
        routings. check is one of: no_bill (make items with no primary
        bill), no_routing (items with a bill but no routing),
        inactive_components (effective components whose item is not BOM
        enabled), loops (assemblies that are, directly or indirectly, their
        own component; walks every bill to 20 levels and can be slow on a
        large organization). Capped at 50 rows with total_count. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"check": check, "organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="bom_integrity_check", params=params, org_ids=org_ids,
            instance=instance,
            build=lambda eff: build_integrity_query(eff, organization_code, check),
            key="findings",
        )


ENGINEERING_TOOLSET = ToolSet(name="mfg_engineering", register=_register)
