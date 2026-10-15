"""Shared pieces for the functional (Org-ID-scoped) tool families — scm,
manufacturing. These tools read business data and so run as
target_system="ebs": every query is restricted to the caller's effective Org
IDs (operating units), never the ebs_dba all-or-nothing grant.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.mcpserver.exceptions import ToolError

from ebsmcp.tools.registry import ToolContext, resolve_scoped_call, split_total_count

ROW_CAP = 50

# Domain preference orders (see identity.pick_by_domain) for the functional
# families. scm and manufacturing share data and responsibilities, so each
# accepts the other's mapping, preferring its own; "other" catches
# responsibilities from applications outside the known pillars. Neither
# accepts finance or hcm — those mappings are never selected here.
SCM_DOMAINS: tuple[str, ...] = ("scm", "manufacturing", "other")
MANUFACTURING_DOMAINS: tuple[str, ...] = ("manufacturing", "scm", "other")


def org_in_clause(column: str, org_ids: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    """`column IN (:scm_org_0, :scm_org_1, ...)` with one bind per Org ID.

    Org IDs reach us as strings (ResolvedIdentity.allowed_org_ids) but the
    columns are NUMBER; converting here keeps the bind type honest instead of
    leaning on Oracle's implicit conversion. An empty set is refused rather
    than rendered as `IN ()`, which is a syntax error at best — the
    entitlement filter already guarantees non-empty for the "ebs" persona.
    """
    if not org_ids:
        raise ToolError("No effective Org IDs to scope this query to.")
    binds: dict[str, Any] = {}
    names = []
    for i, org_id in enumerate(org_ids):
        try:
            binds[f"scm_org_{i}"] = int(org_id)
        except ValueError as exc:
            raise ToolError(f"Org ID {org_id!r} is not numeric.") from exc
        names.append(f":scm_org_{i}")
    return f"{column} IN ({', '.join(names)})", binds


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so a caller-supplied prefix matches literally.
    Pair with `ESCAPE '\\'` in the SQL."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def scoped_envelope(
    ctx, identity, effective_org_ids, filters, rows, total, key, row_cap: int = ROW_CAP
) -> dict:
    """The response shape every SCM list tool returns."""
    return {
        "environment": ctx.environment,
        "mapped_role": identity.mapped_role,
        "effective_org_ids": list(effective_org_ids),
        "filters": filters,
        "row_cap": row_cap,
        "total_count": total,
        key: rows,
    }


def inventory_org_filter(
    org_ids: tuple[str, ...], organization_code: str | None
) -> tuple[list[str], dict[str, Any]]:
    """WHERE fragments scoping a query to inventory orgs under the caller's
    operating units (via an `ood` alias for ORG_ORGANIZATION_DEFINITIONS),
    optionally narrowed to one inventory organization code."""
    scope, binds = org_in_clause("ood.operating_unit", org_ids)
    where = [scope]
    if organization_code:
        where.append("ood.organization_code = :organization_code")
        binds["organization_code"] = organization_code.upper()
    return where, binds


def run_scoped_list(
    ctx: ToolContext,
    *,
    tool_name: str,
    params: dict[str, Any],
    org_ids: list[str] | None,
    instance: str | None,
    build: Callable[[tuple[str, ...]], tuple[str, dict[str, Any]]],
    key: str,
    domains: tuple[str, ...] | None = None,
    row_cap: int = ROW_CAP,
    annotate: Callable[[list[dict]], list[dict]] | None = None,
    summarize: Callable[[list[dict], int | None], dict] | None = None,
) -> dict:
    """resolve_scoped_call + run + lift total_count + envelope, for the
    common single-query list tool. `build` receives the effective Org IDs and
    returns (sql, binds); annotate/summarize are pure post-processing so the
    model never has to infer a code or a verdict from raw rows."""
    with resolve_scoped_call(
        ctx, tool_name=tool_name, target_system="ebs", params=params,
        requested_org_ids=org_ids, requested_instance=instance, domains=domains,
    ) as (identity, effective_org_ids, connector):
        sql, binds = build(effective_org_ids)
        rows, total = split_total_count(connector.run(sql, binds))
        if annotate:
            rows = annotate(rows)
        body = scoped_envelope(
            ctx, identity, effective_org_ids, params, rows, total, key, row_cap
        )
        if summarize:
            body.update(summarize(rows, total))
        return body
