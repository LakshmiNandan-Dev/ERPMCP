"""object_ddl: extract the CREATE statement for database objects.

DBMS_METADATA.GET_DDL is the Oracle-supported way to do this and works for
every object kind a DBA asks about (tables, views, indexes, sequences,
synonyms, PL/SQL, types, triggers, materialized views, ...). The tool is a
two-step read, both steps pure SELECTs:

1. list the objects matching owner / name / type from DBA_OBJECTS (capped);
2. one GET_DDL call per object, each isolated, so a single unsupported or
   unreadable object (ORA-31603, ORA-31608, no SELECT_CATALOG_ROLE for that
   schema) is reported against that object instead of failing the lot.

The search reads DBA_OBJECTS or ALL_OBJECTS (`catalog`), and needs at least one
of owner / name / prefix / substring: "every object in the database" is tens
of thousands of objects and megabytes of text, so a search is always bounded. Output is bounded three ways — number of
objects, characters per object, and a total character budget — and every
truncation is flagged in the response, never silent.

Not extractable by design: DATABASE LINK and USER, whose DDL can carry
credentials (password hashes / IDENTIFIED BY clauses). They are excluded from
the listing, not filtered afterwards. PL/SQL created with the wrap utility
comes back wrapped; that is reported as `wrapped: true`.

Confidence: run against the live R12.2 instance on 2026-10-21 (DEV, through
the read-only account): synonym, package body (up to 184k chars) and view DDL
extracted correctly with CLOB returned as text. In APPS most "tables" are
synonyms onto APPLSYS/AR/AP..., so ask for owner=APPLSYS (etc.) to get a
table's own CREATE TABLE. Not yet exercised live: tables, indexes, triggers,
types, sequences, materialized views, and the per-object error path.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call, split_total_count
from ebsmcp.tools.scoping import escape_like

# DBA_OBJECTS.OBJECT_TYPE values this tool will extract. Hardcoded, never
# built from caller input. DATABASE LINK / USER are deliberately absent.
SUPPORTED_TYPES: tuple[str, ...] = (
    "TABLE", "VIEW", "MATERIALIZED VIEW", "SEQUENCE", "SYNONYM", "INDEX",
    "TRIGGER", "PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "TYPE",
    "TYPE BODY", "LIBRARY", "JAVA SOURCE",
)

# Which dictionary view the search reads. DBA_OBJECTS sees every object but
# needs a privileged account; ALL_OBJECTS sees only what the connecting
# account may access, and works without SELECT_CATALOG_ROLE. A fixed mapping,
# never built from caller text.
Catalog = Literal["DBA", "ALL"]
_CATALOG_VIEW = {"DBA": "DBA_OBJECTS", "ALL": "ALL_OBJECTS"}

DEFAULT_MAX_OBJECTS = 5
MAX_OBJECTS = 25
DEFAULT_MAX_CHARS = 50_000
MAX_CHARS = 200_000
# Ceiling on DDL text across one response, whatever the per-object settings.
TOTAL_CHAR_BUDGET = 500_000


def metadata_type(object_type: str) -> str:
    """DBA_OBJECTS spells types with spaces ('PACKAGE BODY'); DBMS_METADATA
    wants underscores ('PACKAGE_BODY'). Raises for anything not extractable."""
    t = object_type.strip().upper()
    if t not in SUPPORTED_TYPES:
        raise ToolError(
            f"object_type {object_type!r} is not extractable. Supported: "
            + ", ".join(SUPPORTED_TYPES)
            + ". DATABASE LINK and USER are excluded because their DDL can carry credentials."
        )
    return t.replace(" ", "_")


def clamp(value: int | None, default: int, ceiling: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), ceiling))


def build_ddl_object_list_query(
    owner: str | None,
    object_name: str | None,
    name_prefix: str | None,
    object_type: str | None,
    max_objects: int,
    name_contains: str | None = None,
    catalog: Catalog = "DBA",
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Searches DBA_OBJECTS or
    ALL_OBJECTS for extractable objects. At least one of owner, object_name,
    name_prefix or name_contains is required, so a search can never be "every
    object". Recycle-bin (BIN$) and system-generated objects are skipped.
    Plain SELECT, so COUNT(*) OVER () reports every match, not just the page."""
    if catalog not in _CATALOG_VIEW:
        raise ToolError(f"catalog must be one of {sorted(_CATALOG_VIEW)}, got {catalog!r}")
    owner = owner.strip() if owner else None
    if not any((owner, object_name, name_prefix, name_contains)):
        raise ToolError(
            "Give at least one of owner, object_name, name_prefix or name_contains — "
            "an unbounded search over every object is not allowed."
        )
    binds: dict[str, Any] = {}
    where = ["generated = 'N'", "object_name NOT LIKE 'BIN$%'"]
    if owner:
        where.append("owner = :owner")
        binds["owner"] = owner.upper()

    if object_type:
        metadata_type(object_type)  # validates against SUPPORTED_TYPES
        where.append("object_type = :object_type")
        binds["object_type"] = object_type.strip().upper()
    else:
        where.append("object_type IN (" + ", ".join(f"'{t}'" for t in SUPPORTED_TYPES) + ")")
    if object_name:
        where.append("object_name = :object_name")
        binds["object_name"] = object_name.strip().upper()
    if name_prefix:
        where.append("object_name LIKE :name_prefix ESCAPE '\\'")
        binds["name_prefix"] = escape_like(name_prefix.strip().upper()) + "%"
    if name_contains:
        where.append("object_name LIKE :name_contains ESCAPE '\\'")
        binds["name_contains"] = "%" + escape_like(name_contains.strip().upper()) + "%"
    binds["max_objects"] = max_objects

    sql = (
        "SELECT owner, object_name, object_type, status, last_ddl_time, "
        "COUNT(*) OVER () AS total_count "
        f"FROM {_CATALOG_VIEW[catalog]} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY object_type, object_name "
        "FETCH FIRST :max_objects ROWS ONLY"
    )
    return sql, binds


def build_ddl_query(
    owner: str, object_name: str, object_type: str
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. All three values are bound."""
    return (
        "SELECT DBMS_METADATA.GET_DDL(:object_type, :object_name, :owner) AS ddl FROM DUAL",
        {
            "object_type": metadata_type(object_type),
            "object_name": object_name,
            "owner": owner,
        },
    )


def annotate_ddl(ddl: str | None, max_chars: int) -> dict[str, Any]:
    """Decide in Python how a DDL text is reported: stripped, bounded, with
    its true length, a truncation flag and whether it is wrapped PL/SQL."""
    text = (ddl or "").strip()
    wrapped = " wrapped" in text[:400].lower() and "create" in text[:400].lower()
    truncated = len(text) > max_chars
    return {
        "ddl": text[:max_chars],
        "ddl_chars": len(text),
        "truncated": truncated,
        "wrapped": wrapped,
    }


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def object_ddl(
        owner: str | None = None,
        object_name: str | None = None,
        name_prefix: str | None = None,
        name_contains: str | None = None,
        object_type: str | None = None,
        catalog: Catalog = "DBA",
        max_objects: int | None = None,
        max_chars: int | None = None,
        instance: str | None = None,
    ) -> dict:
        """Search for database objects and extract their CREATE DDL (via
        DBMS_METADATA.GET_DDL): tables, views, materialized views, sequences,
        synonyms, indexes, triggers, procedures, functions, packages and
        package bodies, types and type bodies, libraries, Java sources.
        The search reads DBA_OBJECTS (catalog="DBA", the default: every
        object in the database) or ALL_OBJECTS (catalog="ALL": only objects
        the connecting account can access — use it if DBA_OBJECTS is not
        readable). Filter by owner (e.g. APPS, APPLSYS, or PUBLIC for public
        synonyms), object_name (exact), name_prefix, name_contains (anywhere
        in the name) and/or object_type ('PACKAGE BODY', 'TABLE', ...); at
        least one of owner, object_name, name_prefix or name_contains is
        required, so owner is optional: name_contains="FND_USER" searches all
        schemas. Returns up to max_objects (default 5,
        max 25) objects, each cut at max_chars characters (default 50000,
        max 200000) with ddl_chars giving the true length and truncated
        saying whether it was cut; total_count is how many objects matched,
        so page through a large schema by narrowing the prefix or type.
        Objects whose DDL could not be read carry an error instead of ddl.
        Wrapped (obfuscated) PL/SQL is returned wrapped (wrapped: true).
        DATABASE LINK and USER are not extractable — their DDL can carry
        credentials. For status or last compile time use database_objects;
        for compile errors use object_errors. Call list_ebs_instances first
        if unsure which EBS instance names (e.g. PROD, UAT) this deployment
        has configured."""
        n_objects = clamp(max_objects, DEFAULT_MAX_OBJECTS, MAX_OBJECTS)
        n_chars = clamp(max_chars, DEFAULT_MAX_CHARS, MAX_CHARS)
        params = {
            "owner": owner,
            "object_name": object_name,
            "name_prefix": name_prefix,
            "name_contains": name_contains,
            "object_type": object_type,
            "catalog": catalog,
            "max_objects": n_objects,
            "max_chars": n_chars,
        }
        with resolve_scoped_call(
            ctx, tool_name="object_ddl", target_system="ebs_dba", params=params,
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_ddl_object_list_query(
                owner, object_name, name_prefix, object_type, n_objects,
                name_contains, catalog,
            )
            listed, total = split_total_count(connector.run(sql, binds))

            budget = TOTAL_CHAR_BUDGET
            objects: list[dict[str, Any]] = []
            for row in listed:
                entry = {
                    "owner": row["owner"],
                    "object_name": row["object_name"],
                    "object_type": row["object_type"],
                    "status": row["status"],
                    "last_ddl_time": row["last_ddl_time"],
                }
                if budget <= 0:
                    entry["error"] = (
                        "skipped: response character budget used up — "
                        "narrow the request (name_prefix / object_type) and call again"
                    )
                    objects.append(entry)
                    continue
                try:
                    ddl_sql, ddl_binds = build_ddl_query(
                        row["owner"], row["object_name"], row["object_type"]
                    )
                    ddl_rows = connector.run(ddl_sql, ddl_binds)
                    info = annotate_ddl(
                        str(ddl_rows[0]["ddl"]) if ddl_rows else None, min(n_chars, budget)
                    )
                    budget -= len(info["ddl"])
                    entry.update(info)
                except Exception as exc:  # noqa: BLE001 — isolate one object's failure
                    entry["error"] = str(exc).splitlines()[0][:300]
                objects.append(entry)

            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "filters": params,
                "total_count": total,
                "returned": len(objects),
                "objects": objects,
            }


OBJECT_DDL_TOOLSET = ToolSet(name="dba_object_ddl", register=_register)
