"""Shared pieces for SCM tools, which read functional business data and so
run as target_system="ebs": every query is restricted to the caller's
effective Org IDs (operating units), never the ebs_dba all-or-nothing grant.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

ROW_CAP = 50


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


def scoped_envelope(ctx, identity, effective_org_ids, filters, rows, total, key) -> dict:
    """The response shape every SCM list tool returns."""
    return {
        "environment": ctx.environment,
        "mapped_role": identity.mapped_role,
        "effective_org_ids": list(effective_org_ids),
        "filters": filters,
        "row_cap": ROW_CAP,
        "total_count": total,
        key: rows,
    }
