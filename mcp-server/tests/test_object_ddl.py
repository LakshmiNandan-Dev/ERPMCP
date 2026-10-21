"""object_ddl: the pure builders directly, then the tool end to end through
MCP — per-object failure isolation, truncation flags, and the exclusion of
object types whose DDL can carry credentials."""

from __future__ import annotations

import json

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import OBJECT_DDL_TOOLSET, ToolContext, mount_toolsets
from ebsmcp.tools.dba.object_ddl import (
    SUPPORTED_TYPES,
    annotate_ddl,
    build_ddl_object_list_query,
    build_ddl_query,
    clamp,
    metadata_type,
)


def test_list_query_binds_everything_and_is_conventional():
    sql, binds = build_ddl_object_list_query("apps", "my_pkg", "xx_", "package body", 5)
    validate_sql_conventions(sql)
    assert binds == {
        "owner": "APPS", "object_type": "PACKAGE BODY", "object_name": "MY_PKG",
        "name_prefix": "XX\\_%", "max_objects": 5,
    }
    for v in ("apps", "my_pkg", "package body"):
        assert v not in sql
    assert "FETCH FIRST :max_objects ROWS ONLY" in sql
    assert "COUNT(*) OVER () AS total_count" in sql and "DISTINCT" not in sql


def test_unfiltered_type_uses_the_fixed_supported_list_only():
    sql, binds = build_ddl_object_list_query("APPS", None, None, None, 5)
    assert "object_type" not in binds
    for t in SUPPORTED_TYPES:
        assert f"'{t}'" in sql
    assert "DATABASE LINK" not in sql and "'USER'" not in sql
    assert "generated = 'N'" in sql and "BIN$" in sql


def test_an_unbounded_search_is_refused():
    for args in ((None, None, None, None, 5), ("  ", None, None, "TABLE", 5)):
        with pytest.raises(ToolError, match="at least one of"):
            build_ddl_object_list_query(*args)


def test_owner_is_optional_when_another_filter_bounds_the_search():
    sql, binds = build_ddl_object_list_query(None, None, None, None, 5, name_contains="fnd_us")
    validate_sql_conventions(sql)
    assert "owner = :owner" not in sql and "owner" not in binds
    assert binds["name_contains"] == "%FND\\_US%" and "fnd" not in sql.lower().split("where")[1]


def test_catalog_selects_dba_or_all_objects():
    dba, _ = build_ddl_object_list_query("APPS", None, None, None, 5)
    all_, _ = build_ddl_object_list_query("APPS", None, None, None, 5, catalog="ALL")
    assert "FROM DBA_OBJECTS" in dba and "FROM ALL_OBJECTS" in all_
    validate_sql_conventions(dba)
    validate_sql_conventions(all_)
    with pytest.raises(ToolError, match="catalog must be"):
        build_ddl_object_list_query("APPS", None, None, None, 5, catalog="SYS.USER$")  # type: ignore[arg-type]


def test_only_all_objects_is_exempt_from_qualification():
    validate_sql_conventions("SELECT 1 FROM ALL_OBJECTS")
    with pytest.raises(Exception):
        validate_sql_conventions("SELECT 1 FROM ALL_ORDERS")


@pytest.mark.parametrize("bad", ["DATABASE LINK", "USER", "TABLE; DROP", "ROLE"])
def test_credential_bearing_and_unknown_types_are_refused(bad):
    with pytest.raises(ToolError, match="not extractable"):
        metadata_type(bad)
    with pytest.raises(ToolError):
        build_ddl_object_list_query("APPS", None, None, bad, 5)


def test_metadata_type_uses_underscores():
    assert metadata_type("package body") == "PACKAGE_BODY"
    assert metadata_type("MATERIALIZED VIEW") == "MATERIALIZED_VIEW"


def test_ddl_query_binds_all_three_values():
    sql, binds = build_ddl_query("APPS", "FND_USER", "TABLE")
    validate_sql_conventions(sql)
    assert "DBMS_METADATA.GET_DDL(:object_type, :object_name, :owner)" in sql
    assert binds == {"object_type": "TABLE", "object_name": "FND_USER", "owner": "APPS"}


def test_clamp():
    assert clamp(None, 5, 25) == 5 and clamp(0, 5, 25) == 1
    assert clamp(999, 5, 25) == 25 and clamp(10, 5, 25) == 10


def test_annotate_ddl_truncates_and_reports_true_length():
    out = annotate_ddl("  CREATE TABLE t (a NUMBER)  \n", 10)
    assert out["ddl"] == "CREATE TAB" and out["ddl_chars"] == 25 and out["truncated"] is True
    assert annotate_ddl("CREATE X", 100)["truncated"] is False
    assert annotate_ddl(None, 10) == {"ddl": "", "ddl_chars": 0, "truncated": False, "wrapped": False}


def test_wrapped_plsql_is_flagged():
    assert annotate_ddl("CREATE OR REPLACE PACKAGE BODY p wrapped\na000000\n", 999)["wrapped"]
    assert not annotate_ddl("CREATE OR REPLACE PACKAGE BODY p AS END;", 999)["wrapped"]


# ── end to end ───────────────────────────────────────────────────────────────


class Scripted(EBSConnector):
    def __init__(self, listed, ddl):
        self.listed, self.ddl, self.calls = listed, ddl, []

    def execute_query(self, sql, binds=None):
        self.calls.append((sql, binds))
        if "DBMS_METADATA" in sql:
            r = self.ddl[binds["object_name"]]
            if isinstance(r, Exception):
                raise r
            return [{"ddl": r}]
        return self.listed


async def call(conn, args):
    app = MCPServer("t")
    ctx = ToolContext(
        connectors={"TEST": conn},
        identity_resolver=StubIdentityResolver(ResolvedIdentity(
            subject="u@corp.com", environment="test", target_system="ebs_dba",
            mapped_role="DBA", allowed_org_ids=(),
        )),
        entitlement=EntitlementFilter(), audit=AuditLogger(),
        environment="test", dev_subject="u@corp.com",
    )
    mount_toolsets(app, ctx, [OBJECT_DDL_TOOLSET])
    async with InMemoryTransport(app)._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("object_ddl", args)


def row(name, typ="TABLE", total=3):
    return {"owner": "APPS", "object_name": name, "object_type": typ, "status": "VALID",
            "last_ddl_time": "2026-01-01", "total_count": total}


@pytest.mark.asyncio
async def test_one_failing_object_does_not_fail_the_call():
    conn = Scripted(
        [row("A"), row("B"), row("C", "PACKAGE BODY")],
        {"A": "CREATE TABLE a (x NUMBER)", "B": RuntimeError("ORA-31603: object not found\nmore"),
         "C": "x" * 500},
    )
    result = await call(conn, {"owner": "apps", "max_chars": 100})
    assert not result.is_error, result.content[0].text
    body = json.loads(result.content[0].text)
    a, b, c = body["objects"]
    assert body["total_count"] == 3 and body["returned"] == 3
    assert a["ddl"] == "CREATE TABLE a (x NUMBER)" and a["truncated"] is False
    assert "ddl" not in b and b["error"] == "ORA-31603: object not found"
    assert c["truncated"] is True and c["ddl_chars"] == 500 and len(c["ddl"]) == 100
    # type was translated for DBMS_METADATA
    assert conn.calls[-1][1]["object_type"] == "PACKAGE_BODY"


@pytest.mark.asyncio
async def test_unextractable_type_is_an_error_not_a_query():
    conn = Scripted([], {})
    result = await call(conn, {"owner": "APPS", "object_type": "DATABASE LINK"})
    assert result.is_error and conn.calls == []


@pytest.mark.asyncio
async def test_unbounded_search_is_an_error_over_the_wire():
    conn = Scripted([], {})
    result = await call(conn, {})
    assert result.is_error and conn.calls == []


@pytest.mark.asyncio
async def test_search_without_owner_across_all_objects():
    conn = Scripted([row("FND_USER")], {"FND_USER": "CREATE TABLE x"})
    result = await call(conn, {"name_contains": "fnd_user", "catalog": "ALL"})
    assert not result.is_error, result.content[0].text
    assert "FROM ALL_OBJECTS" in conn.calls[0][0]
    assert json.loads(result.content[0].text)["objects"][0]["ddl"] == "CREATE TABLE x"


@pytest.mark.asyncio
async def test_caps_are_clamped():
    conn = Scripted([], {})
    await call(conn, {"owner": "APPS", "max_objects": 9999})
    assert conn.calls[0][1]["max_objects"] == 25
