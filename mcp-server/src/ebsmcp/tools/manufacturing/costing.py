"""Costing: item cost by element, job variance, uncosted transactions and
WIP value.

Scoping: cost data carries the inventory ORGANIZATION_ID, scoped through
APPS.ORG_ORGANIZATION_DEFINITIONS.OPERATING_UNIT. Costs are per organization
so item_cost_breakdown and job_variance require/accept organization_code.

Confidence: written from the documented R12 data model and NOT yet run
against a live instance (unit-tested query construction only). Assumptions
to verify: CST_ITEM_COST_DETAILS.COST_ELEMENT_ID (1 material, 2 material
overhead, 3 resource, 4 outside processing, 5 overhead) and LEVEL_TYPE
(1 this level, 2 previous level); MTL_MATERIAL_TRANSACTIONS.COSTED_FLAG 'N'
pending / 'E' error; and the full WIP_PERIOD_BALANCES column set
(TL_/PL_ x MATERIAL, MATERIAL_OVERHEAD, RESOURCE, OVERHEAD,
OUTSIDE_PROCESSING x _IN/_OUT/_VAR) — a misspelt column there fails the
whole query with ORA-00904, so check that first. job_variance and
wip_value_summary read cumulative period balances, not individual
transactions: "in" is charged to the job, "out" is relieved at standard,
"variance" is posted at close. uncosted_transactions scans
MTL_MATERIAL_TRANSACTIONS and can be slow on a large instance; it is bounded
by since_days.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call
from ebsmcp.tools.scoping import MANUFACTURING_DOMAINS, ROW_CAP, inventory_org_filter, run_scoped_list

COST_ELEMENT_LABELS = {
    1: "Material",
    2: "Material Overhead",
    3: "Resource",
    4: "Outside Processing",
    5: "Overhead",
}
LEVEL_LABELS = {1: "This Level", 2: "Previous Level"}
COSTED_FLAG_LABELS = {"N": "Pending", "E": "Error"}

# WIP_PERIOD_BALANCES element stems, in the order they are reported.
WPB_ELEMENTS = ("material", "material_overhead", "resource", "overhead", "outside_processing")


def _wpb_sums() -> str:
    """SUM(TL_x + PL_x) for in/out/var per element, aliased x_in/x_out/x_var."""
    parts = []
    for element in WPB_ELEMENTS:
        for flow in ("in", "out", "var"):
            parts.append(
                f"SUM(NVL(wpb.tl_{element}_{flow}, 0) + NVL(wpb.pl_{element}_{flow}, 0)) "
                f"AS {element}_{flow}"
            )
    return ", ".join(parts)


def build_item_cost_query(
    org_ids: tuple[str, ...],
    organization_code: str,
    item_number: str,
    cost_type: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Item cost by cost type, element
    and level."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("msi.segment1 = :item_number")
    binds["item_number"] = item_number
    if cost_type:
        where.append("cct.cost_type = :cost_type")
        binds["cost_type"] = cost_type

    sql = (
        "SELECT cct.cost_type, cicd.cost_element_id, cicd.level_type, "
        "SUM(cicd.item_cost) AS item_cost, COUNT(*) OVER () AS total_count "
        "FROM APPS.CST_ITEM_COST_DETAILS cicd "
        "JOIN APPS.CST_COST_TYPES cct ON cct.cost_type_id = cicd.cost_type_id "
        "JOIN APPS.MTL_SYSTEM_ITEMS_B msi ON msi.inventory_item_id = cicd.inventory_item_id "
        "AND msi.organization_id = cicd.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = cicd.organization_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY cct.cost_type, cicd.cost_element_id, cicd.level_type "
        "ORDER BY cct.cost_type, cicd.cost_element_id, cicd.level_type "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_item_cost(rows: list[dict]) -> list[dict]:
    return [
        {
            **r,
            "cost_element": COST_ELEMENT_LABELS.get(r.get("cost_element_id"), "Unknown"),
            "level": LEVEL_LABELS.get(r.get("level_type"), "Unknown"),
        }
        for r in rows
    ]


def build_job_variance_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    job_name: str,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. One aggregate row of
    in/out/variance per cost element for one job. No window total — an
    aggregate without GROUP BY is always exactly one row."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where.append("we.wip_entity_name = :job_name")
    binds["job_name"] = job_name

    sql = (
        f"SELECT {_wpb_sums()} "
        "FROM APPS.WIP_PERIOD_BALANCES wpb "
        "JOIN APPS.WIP_ENTITIES we ON we.wip_entity_id = wpb.wip_entity_id "
        "AND we.organization_id = wpb.organization_id "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = wpb.organization_id "
        f"WHERE {' AND '.join(where)}"
    )
    return sql, binds


def summarize_job_variance(rows: list[dict]) -> dict:
    """Reshape the single aggregate row into one entry per cost element and
    decide the totals here, so the model never does the arithmetic.
    found=False when the job has no period balances at all (an aggregate
    over zero rows returns one all-NULL row, which must not read as zero)."""
    row = rows[0] if rows else {}
    if not row or all(v is None for v in row.values()):
        return {"found": False, "elements": [], "totals": None}

    elements = []
    totals = {"in": 0.0, "out": 0.0, "var": 0.0}
    for element in WPB_ELEMENTS:
        charged = float(row.get(f"{element}_in") or 0)
        relieved = float(row.get(f"{element}_out") or 0)
        variance = float(row.get(f"{element}_var") or 0)
        totals["in"] += charged
        totals["out"] += relieved
        totals["var"] += variance
        elements.append(
            {
                "cost_element": element.replace("_", " ").title(),
                "charged": charged,
                "relieved_at_standard": relieved,
                "variance_posted": variance,
                "unrelieved_balance": charged - relieved - variance,
            }
        )
    totals["unrelieved_balance"] = totals["in"] - totals["out"] - totals["var"]
    return {"found": True, "elements": elements, "totals": totals}


def build_uncosted_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
    since_days: int,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Material transactions the cost
    manager has not costed (pending or in error), grouped by type and error."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    where += ["mmt.costed_flag IN ('N', 'E')", "mmt.transaction_date >= SYSDATE - :since_days"]
    binds["since_days"] = since_days

    sql = (
        "SELECT ood.organization_code, mmt.costed_flag, mtt.transaction_type_name, "
        "mmt.error_code, mmt.error_explanation, COUNT(*) AS transaction_count, "
        "MIN(mmt.transaction_date) AS oldest_transaction_date, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.MTL_MATERIAL_TRANSACTIONS mmt "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = mmt.organization_id "
        "JOIN APPS.MTL_TRANSACTION_TYPES mtt ON mtt.transaction_type_id = mmt.transaction_type_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY ood.organization_code, mmt.costed_flag, mtt.transaction_type_name, "
        "mmt.error_code, mmt.error_explanation "
        "ORDER BY transaction_count DESC "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def annotate_uncosted(rows: list[dict]) -> list[dict]:
    return [
        {**r, "cost_status": COSTED_FLAG_LABELS.get(r.get("costed_flag"), "Unknown")} for r in rows
    ]


def build_wip_value_query(
    org_ids: tuple[str, ...],
    organization_code: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Net WIP balance by accounting
    class (charged - relieved - variance, all periods; closed jobs net to
    zero because their variance is posted)."""
    where, binds = inventory_org_filter(org_ids, organization_code)
    net = " + ".join(
        f"SUM(NVL(wpb.tl_{e}_in, 0) + NVL(wpb.pl_{e}_in, 0) "
        f"- NVL(wpb.tl_{e}_out, 0) - NVL(wpb.pl_{e}_out, 0) "
        f"- NVL(wpb.tl_{e}_var, 0) - NVL(wpb.pl_{e}_var, 0))"
        for e in WPB_ELEMENTS
    )
    per_element = ", ".join(
        f"SUM(NVL(wpb.tl_{e}_in, 0) + NVL(wpb.pl_{e}_in, 0) "
        f"- NVL(wpb.tl_{e}_out, 0) - NVL(wpb.pl_{e}_out, 0) "
        f"- NVL(wpb.tl_{e}_var, 0) - NVL(wpb.pl_{e}_var, 0)) AS {e}_balance"
        for e in WPB_ELEMENTS
    )
    sql = (
        "SELECT ood.organization_code, wpb.class_code, "
        f"{per_element}, ({net}) AS total_wip_value, "
        "COUNT(*) OVER () AS total_count "
        "FROM APPS.WIP_PERIOD_BALANCES wpb "
        "JOIN APPS.ORG_ORGANIZATION_DEFINITIONS ood ON ood.organization_id = wpb.organization_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY ood.organization_code, wpb.class_code "
        "ORDER BY ood.organization_code, wpb.class_code "
        f"FETCH FIRST {ROW_CAP} ROWS ONLY"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

    @app.tool(annotations=read_only)
    def item_cost_breakdown(
        item_number: str,
        organization_code: str,
        cost_type: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Break an item's cost down by cost element (material, material
        overhead, resource, outside processing, overhead) and level (this
        level or previous level) for each cost type, or one named cost type
        such as Frozen. Costs are per inventory organization, so
        organization_code is required. Call list_ebs_instances first if
        unsure which EBS instance names this deployment has.
        """
        params = {
            "item_number": item_number, "organization_code": organization_code,
            "cost_type": cost_type, "org_ids": org_ids,
        }
        return run_scoped_list(
            ctx, tool_name="item_cost_breakdown", params=params, org_ids=org_ids,
            instance=instance,
            build=lambda eff: build_item_cost_query(eff, organization_code, item_number, cost_type),
            key="costs", domains=MANUFACTURING_DOMAINS, annotate=annotate_item_cost,
        )

    @app.tool(annotations=read_only)
    def job_variance(
        job_name: str,
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report one job's cost position by cost element: charged to the
        job, relieved at standard, variance posted, and the unrelieved
        balance, with totals. Reads cumulative WIP period balances. found
        is false when the job has no balances. Pass organization_code if
        the job name exists in more than one organization. Call
        list_ebs_instances first if unsure which EBS instance names this
        deployment has.
        """
        params = {"job_name": job_name, "organization_code": organization_code, "org_ids": org_ids}
        with resolve_scoped_call(
            ctx, tool_name="job_variance", target_system="ebs", domains=MANUFACTURING_DOMAINS, params=params,
            requested_org_ids=org_ids, requested_instance=instance,
        ) as (identity, effective_org_ids, connector):
            sql, binds = build_job_variance_query(effective_org_ids, organization_code, job_name)
            rows = connector.run(sql, binds)
            body = {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "effective_org_ids": list(effective_org_ids),
                "filters": params,
            }
            body.update(summarize_job_variance(rows))
            return body

    @app.tool(annotations=read_only)
    def uncosted_transactions(
        organization_code: str | None = None,
        since_days: int = 90,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report material transactions the cost manager has not processed
        in the last since_days days (default 90), grouped by transaction
        type and error, with count and oldest date. cost_status is Pending
        (not yet picked up) or Error (failed, with error text). Resource
        and move transactions stuck in the WIP interface are in
        wip_pending_transactions. Capped at 50 groups with total_count.
        Call list_ebs_instances first if unsure which EBS instance names
        this deployment has.
        """
        params = {"organization_code": organization_code, "since_days": since_days, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="uncosted_transactions", params=params, org_ids=org_ids,
            instance=instance,
            build=lambda eff: build_uncosted_query(eff, organization_code, since_days),
            key="uncosted", domains=MANUFACTURING_DOMAINS, annotate=annotate_uncosted,
        )

    @app.tool(annotations=read_only)
    def wip_value_summary(
        organization_code: str | None = None,
        org_ids: list[str] | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report net WIP value by accounting class per inventory
        organization, split by cost element, with a total. Read from
        cumulative WIP period balances. Capped at 50 groups with
        total_count. Call list_ebs_instances first if unsure which EBS
        instance names this deployment has.
        """
        params = {"organization_code": organization_code, "org_ids": org_ids}
        return run_scoped_list(
            ctx, tool_name="wip_value_summary", params=params, org_ids=org_ids,
            instance=instance, build=lambda eff: build_wip_value_query(eff, organization_code),
            key="wip_value", domains=MANUFACTURING_DOMAINS,
        )


COSTING_TOOLSET = ToolSet(name="mfg_costing", register=_register)
