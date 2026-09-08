"""HA/DR health: one parameterized tool over three independent, fixed
queries — not a shared WHERE clause, since dataguard_lag/restore_points/
rac_health read from unrelated views with unrelated row shapes. Same
check-selects-a-fixed-query shape as redo_archive_backup.py's
get_query().

Conditional on topology, per the architecture doc: Data Guard and RAC
are separately licensed Oracle options. These only return meaningful
data where one is already in use — nothing here implies newly licensing
either just for monitoring.

SQL confidence:
- dataguard_lag (GV$DATAGUARD_STATS): MEDIUM-HIGH — standard, documented
  view; 'transport lag'/'apply lag' are the two values every Data Guard
  monitoring reference cites for this exact check.
- restore_points (GV$RESTORE_POINT): HIGH — standard, textbook view.
- rac_health (GV$CLUSTER_INTERCONNECTS): MEDIUM, narrower than the
  catalog's own description. The doc also cites GV$SYSSTAT for
  per-instance load, deliberately NOT included here — GV$SYSSTAT is a
  huge name/value stats view and there isn't confident-enough footing on
  which specific global-cache stat names are the right ones to assert
  without a real RAC instance to check against (same reasoning as
  deferring queue_depth_by_manager etc. in concurrent_processing.py).
"""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

HADRCheck = Literal["dataguard_lag", "restore_points", "rac_health"]

_QUERIES: dict[HADRCheck, str] = {
    "dataguard_lag": (
        "SELECT inst_id, name, value, unit, time_computed "
        "FROM GV$DATAGUARD_STATS "
        "WHERE name IN ('transport lag', 'apply lag') "
        "ORDER BY inst_id, name"
    ),
    # GUARANTEE_FLAG doesn't exist on GV$RESTORE_POINT — verified against
    # a live instance (2026-09-02), ORA-00904. The real column is
    # GUARANTEE_FLASHBACK_DATABASE.
    "restore_points": (
        "SELECT inst_id, name, scn, time, guarantee_flashback_database, storage_size "
        "FROM GV$RESTORE_POINT "
        "ORDER BY time DESC"
    ),
    "rac_health": (
        "SELECT inst_id, name AS interconnect_name, ip_address, is_public, source "
        "FROM GV$CLUSTER_INTERCONNECTS "
        "ORDER BY inst_id"
    ),
}


def get_query(check: HADRCheck) -> str:
    """Pure function, unit-tested directly — see
    test_high_availability_dr_query.py."""
    return _QUERIES[check]


ServicesView = Literal["active", "definitions"]

_SERVICES_QUERIES: dict[ServicesView, str] = {
    "active": (
        "SELECT inst_id, service_id, name, network_name, goal, blocked, clb_goal "
        "FROM GV$ACTIVE_SERVICES "
        "ORDER BY inst_id, name"
    ),
    "definitions": (
        "SELECT service_id, name, network_name, creation_date, goal, clb_goal "
        "FROM DBA_SERVICES "
        "ORDER BY name"
    ),
}


def get_services_query(view: ServicesView) -> str:
    """Pure function, unit-tested directly — see
    test_high_availability_dr_query.py."""
    return _SERVICES_QUERIES[view]


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def ha_dr_status(check: HADRCheck = "dataguard_lag", instance: str | None = None) -> dict:
        """Report Data Guard transport/apply lag (dataguard_lag),
        guaranteed restore points (restore_points), or RAC interconnect
        topology (rac_health). Only meaningful where that topology
        (Data Guard or RAC) is actually in use. Defaults to dataguard_lag.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx, tool_name="ha_dr_status", target_system="ebs_dba", params={"check": check},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_query(check))

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "check": check,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def database_services(view: ServicesView = "active", instance: str | None = None) -> dict:
        """Report which database services are actually running, on
        which instance, right now (active), or every statically
        configured service regardless of whether it's currently running
        anywhere (definitions). Services are the connection-routing/
        load-balancing layer that moves between RAC instances on
        failover. Defaults to active. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="database_services", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_services_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }


HIGH_AVAILABILITY_DR_TOOLSET = ToolSet(name="high_availability_dr", register=_register)
