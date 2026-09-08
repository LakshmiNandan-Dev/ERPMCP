"""Memory (SGA & PGA) health: 5 tools covering the catalog's 8 entries
(sga_status and pga_status each absorb 2 catalog entries via a view
param; memory_advisor combines its 3 advisor views into one report,
since the catalog lists it as a single tool, not per-advisor views).

SQL confidence: HIGH throughout except memory_advisor (MEDIUM — three
less-universally-memorized advisor views combined, worth a spot-check)
and workarea_profile (MEDIUM-HIGH — deliberately doesn't classify
optimal/one-pass/multi-pass, since that needs more than
GV$SQL_WORKAREA_ACTIVE alone reliably provides; returns raw columns
instead of a computed label I'm not fully confident in).
"""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

SgaView = Literal["overview", "pool_detail"]

_SGA_QUERIES: dict[SgaView, str] = {
    "overview": (
        "SELECT inst_id, 'SGA' AS source, name, ROUND(value / 1024 / 1024, 1) AS value_mb "
        "FROM GV$SGA "
        "UNION ALL "
        "SELECT inst_id, 'SGAINFO' AS source, name, ROUND(bytes / 1024 / 1024, 1) AS value_mb "
        "FROM GV$SGAINFO "
        "ORDER BY source, name"
    ),
    "pool_detail": (
        "SELECT inst_id, pool, name, ROUND(bytes / 1024 / 1024, 1) AS value_mb "
        "FROM GV$SGASTAT "
        "WHERE name = 'free memory' "
        "ORDER BY inst_id, pool"
    ),
}


def get_sga_query(view: SgaView) -> str:
    return _SGA_QUERIES[view]


PgaView = Literal["overview", "by_session"]

_PGA_QUERIES: dict[PgaView, str] = {
    "overview": (
        "SELECT inst_id, name, value, unit "
        "FROM GV$PGASTAT "
        "WHERE name IN ('total PGA allocated', 'total PGA inuse', 'maximum PGA allocated', 'over allocation count') "
        "ORDER BY inst_id, name"
    ),
    "by_session": (
        "SELECT s.sid, s.serial#, s.username, p.pga_used_mem, p.pga_alloc_mem, p.pga_max_mem "
        "FROM GV$PROCESS p "
        "JOIN GV$SESSION s ON s.paddr = p.addr AND s.inst_id = p.inst_id "
        "WHERE p.pga_used_mem > 0 "
        "ORDER BY p.pga_alloc_mem DESC "
        "FETCH FIRST 20 ROWS ONLY"
    ),
}


def get_pga_query(view: PgaView) -> str:
    return _PGA_QUERIES[view]


CacheType = Literal["library", "buffer"]

_CACHE_QUERIES: dict[CacheType, str] = {
    "library": (
        "SELECT inst_id, namespace, gets, gethits, ROUND(gethitratio * 100, 1) AS gethit_pct, "
        "pins, pinhits, ROUND(pinhitratio * 100, 1) AS pinhit_pct, reloads, invalidations "
        "FROM GV$LIBRARYCACHE "
        "ORDER BY inst_id, namespace"
    ),
    "buffer": (
        "SELECT inst_id, name AS pool_name, physical_reads, db_block_gets, consistent_gets, "
        "ROUND((1 - physical_reads / NULLIF(db_block_gets + consistent_gets, 0)) * 100, 1) AS buffer_hit_pct "
        "FROM GV$BUFFER_POOL_STATISTICS "
        "ORDER BY inst_id, name"
    ),
}


def get_cache_query(cache: CacheType) -> str:
    return _CACHE_QUERIES[cache]


_SGA_TARGET_ADVICE_SQL = (
    "SELECT inst_id, sga_size, sga_size_factor, estd_db_time, estd_db_time_factor "
    "FROM GV$SGA_TARGET_ADVICE "
    "ORDER BY inst_id, sga_size"
)
_DB_CACHE_ADVICE_SQL = (
    "SELECT inst_id, size_for_estimate, size_factor, estd_physical_read_factor "
    "FROM GV$DB_CACHE_ADVICE "
    "ORDER BY inst_id, size_for_estimate"
)
_SHARED_POOL_ADVICE_SQL = (
    "SELECT inst_id, shared_pool_size_for_estimate, shared_pool_size_factor, estd_lc_load_time "
    "FROM GV$SHARED_POOL_ADVICE "
    "ORDER BY inst_id, shared_pool_size_for_estimate"
)


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def sga_status(view: SgaView = "overview", instance: str | None = None) -> dict:
        """Report SGA component sizes at a glance (overview — GV$SGA +
        GV$SGAINFO), or free memory by pool/component (pool_detail —
        catches shared-pool exhaustion before it becomes ORA-04031).
        Defaults to overview. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="sga_status", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_sga_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def pga_status(view: PgaView = "overview", instance: str | None = None) -> dict:
        """Report aggregate PGA allocated/used/target, including the
        over-allocation count — the strong signal PGA_AGGREGATE_TARGET
        is undersized (overview), or per-session PGA consumption
        (by_session). Defaults to overview. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="pga_status", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_pga_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def cache_efficiency(cache: CacheType = "library", instance: str | None = None) -> dict:
        """Report library cache parse/reuse health (library — a low hit
        ratio usually means literal SQL instead of bind variables
        somewhere), or buffer cache physical-vs-logical read efficiency
        (buffer). Defaults to library. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="cache_efficiency", target_system="ebs_dba", params={"cache": cache},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_cache_query(cache))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "cache": cache,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def memory_advisor(instance: str | None = None) -> dict:
        """Project the effect of resizing SGA, buffer cache, and shared
        pool — the base memory-advisor feature, distinct from ADDM and
        requiring no extra license. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="memory_advisor", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sga_target = connector.run(_SGA_TARGET_ADVICE_SQL)
            buffer_cache = connector.run(_DB_CACHE_ADVICE_SQL)
            shared_pool = connector.run(_SHARED_POOL_ADVICE_SQL)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "sga_target_advice": sga_target,
                "buffer_cache_advice": buffer_cache,
                "shared_pool_advice": shared_pool,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def workarea_profile(instance: str | None = None) -> dict:
        """List currently executing sort/hash-join workareas. A non-null
        tempseg_size means that workarea has spilled to TEMP (not
        optimal) — the direct cause of a session consuming TEMP space.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx, tool_name="workarea_profile", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT sid, inst_id, sql_id, operation_type, policy, actual_mem_used, tempseg_size "
                "FROM GV$SQL_WORKAREA_ACTIVE "
                "ORDER BY tempseg_size DESC NULLS LAST"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "active_workareas": rows,
            }


MEMORY_TOOLSET = ToolSet(name="memory", register=_register)
