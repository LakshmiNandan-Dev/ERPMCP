"""Index health: 2 tools, new category. Nothing in the rest of the DBA
catalog touches indexes at all — this was a genuine gap found by
comparing against oracle-base.com/dba/scripts' Monitoring section rather
than the original architecture-doc catalog.

SQL confidence — both HIGH, columns verified against a live instance
(2026-09-02) before writing either query, not guessed then fixed:
- unusable_indexes: DBA_INDEXES.STATUS = 'UNUSABLE' is the standard,
  textbook check (post-patch/partition-maintenance index breakage).
- non_indexed_foreign_keys: the standard published technique (this exact
  shape appears across most Oracle DBA script collections, including
  oracle-base's own non_indexed_fks.sql) — for each FK's leading column,
  check whether any index on the same table has that column as ITS
  leading column. This is an approximation (it doesn't verify the full
  FK column list is covered, just the first column), but it's the
  well-established "good enough" version everyone actually runs, and a
  missing leading-column index is the version that causes the classic
  child-table-DML-locks-parent-table problem.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def unusable_indexes(instance: str | None = None) -> dict:
        """List up to 50 indexes with STATUS = UNUSABLE — the classic
        post-patch or partition-maintenance breakage that silently turns
        into ORA-01502 on the next DML against the underlying table. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="unusable_indexes", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT owner, index_name, table_owner, table_name, status "
                "FROM DBA_INDEXES "
                "WHERE status = 'UNUSABLE' "
                "ORDER BY owner, index_name "
                "FETCH FIRST 50 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "unusable_indexes": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def non_indexed_foreign_keys(instance: str | None = None) -> dict:
        """List up to 100 foreign key constraints whose leading column
        has no supporting index — the classic cause of unexpected
        full-table locks on the parent table during child-table DML. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="non_indexed_foreign_keys", target_system="ebs_dba", params={},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT fk.owner, fk.table_name, fk.constraint_name AS fk_constraint_name, "
                "fk.column_name "
                "FROM DBA_CONS_COLUMNS fk "
                "JOIN DBA_CONSTRAINTS c "
                "  ON c.owner = fk.owner AND c.constraint_name = fk.constraint_name "
                "  AND c.constraint_type = 'R' "
                "WHERE fk.position = 1 "
                "  AND NOT EXISTS ( "
                "    SELECT 1 FROM DBA_IND_COLUMNS ic "
                "    WHERE ic.index_owner = fk.owner "
                "      AND ic.table_name = fk.table_name "
                "      AND ic.column_name = fk.column_name "
                "      AND ic.column_position = 1 "
                "  ) "
                "ORDER BY fk.owner, fk.table_name "
                "FETCH FIRST 100 ROWS ONLY"
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "non_indexed_foreign_keys": rows,
            }


INDEX_HEALTH_TOOLSET = ToolSet(name="index_health", register=_register)
