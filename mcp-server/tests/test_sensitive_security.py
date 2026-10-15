"""The hcm/finance security framework: domain pinning, deterministic mapping
choice, person (row) scope, field gating, audit disclosure, and the
guardrails that stop a future tool bypassing any of it.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector, validate_sql_conventions
from ebsmcp.identity import (
    ANY_DOMAIN,
    ResolvedIdentity,
    StubIdentityResolver,
    pick_by_domain,
)
from ebsmcp.policy import EntitlementFilter
from ebsmcp.tools import ToolContext, ToolSet, mount_toolsets
from ebsmcp.tools.registry import resolve_scoped_call
from ebsmcp.tools.sensitive import (
    FIELD_CLASSES,
    FieldSpec,
    audit_safe,
    resolve_sensitive_call,
)

SUBJECT = "u@corp.com"


def ident(domain, *, person_id=7, hr_scope="reports", grants=(), orgs=("204",), role="R"):
    return ResolvedIdentity(
        subject=SUBJECT, environment="test", target_system="ebs", mapped_role=role,
        allowed_org_ids=orgs, domain=domain, person_id=person_id, hr_scope=hr_scope,
        sensitive_grants=frozenset(grants),
    )


class Recording(EBSConnector):
    def __init__(self, rows=None):
        self.rows, self.calls = rows or [], []

    def execute_query(self, sql, binds=None):
        self.calls.append((sql, binds))
        return self.rows


class CaptureAudit(AuditLogger):
    def __init__(self):
        super().__init__()
        self.records = []

    def _emit(self, record):
        self.records.append(record)


def make_ctx(*identities, audit=None, connector=None):
    return ToolContext(
        connectors={"TEST": connector or Recording()},
        identity_resolver=StubIdentityResolver(*identities),
        entitlement=EntitlementFilter(),
        audit=audit or CaptureAudit(),
        environment="test",
        dev_subject=SUBJECT,
    )


# ── mapping selection ────────────────────────────────────────────────────────


class _M:
    def __init__(self, domain):
        self.domain = domain


def test_pick_follows_preference_order_not_input_order():
    cands = [_M("scm"), _M("manufacturing")]
    assert pick_by_domain(cands, ("manufacturing", "scm")).domain == "manufacturing"
    assert pick_by_domain(cands, ("scm", "manufacturing")).domain == "scm"


def test_none_never_picks_a_sensitive_domain():
    assert pick_by_domain([_M("hcm"), _M("finance")], None) is None
    assert pick_by_domain([_M("hcm"), _M("scm")], None).domain == "scm"


def test_named_domain_does_not_fall_back_to_another():
    assert pick_by_domain([_M("scm")], ("hcm",)) is None
    assert pick_by_domain([_M("finance")], ("hcm",)) is None


def test_unlabelled_candidate_serves_plain_families_but_never_sensitive():
    assert pick_by_domain([_M(None)], ("scm", "other")) is not None
    assert pick_by_domain([_M(None)], ("hcm",)) is None
    assert pick_by_domain([_M(None)], ("finance",)) is None


def test_any_domain_is_explicit_opt_in():
    assert pick_by_domain([_M("hcm")], ANY_DOMAIN).domain == "hcm"


def test_scm_and_manufacturing_never_receive_an_hcm_or_finance_mapping():
    ctx = make_ctx(ident("hcm"), ident("finance"))
    for domains in (("scm", "manufacturing", "other"), ("manufacturing", "scm", "other"), None):
        with pytest.raises(ToolError, match="No identity mapping"):
            with resolve_scoped_call(
                ctx, tool_name="t", target_system="ebs", params={}, domains=domains
            ):
                pass


# ── entry guardrails ─────────────────────────────────────────────────────────


def test_sensitive_domain_refused_through_plain_resolve_scoped_call():
    ctx = make_ctx(ident("hcm"))
    with pytest.raises(ToolError, match="resolve_sensitive_call"):
        with resolve_scoped_call(
            ctx, tool_name="t", target_system="ebs", params={}, domains=("hcm",)
        ):
            pass


def test_hcm_tool_denied_for_a_user_with_only_scm_and_finance_mappings():
    ctx = make_ctx(ident("scm"), ident("finance"))
    with pytest.raises(ToolError, match="No identity mapping"):
        with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}):
            pass


def test_finance_tool_denied_for_an_hcm_only_user():
    ctx = make_ctx(ident("hcm"))
    with pytest.raises(ToolError, match="No identity mapping"):
        with resolve_sensitive_call(ctx, tool_name="t", domain="finance", params={}):
            pass


def test_resolver_that_ignores_domains_is_still_caught():
    class Sloppy(StubIdentityResolver):
        def resolve(self, subject, target_system, domains=None):
            return self._mappings[0]

    ctx = make_ctx()
    ctx = ToolContext(**{**ctx.__dict__, "identity_resolver": Sloppy(ident("scm"))})
    with pytest.raises(ToolError, match="requires a 'hcm' identity mapping"):
        with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}):
            pass


def test_only_hcm_and_finance_are_valid_sensitive_domains():
    with pytest.raises(ValueError):
        with resolve_sensitive_call(make_ctx(), tool_name="t", domain="scm", params={}):  # type: ignore[arg-type]
            pass


# ── row scope ────────────────────────────────────────────────────────────────


def _scope(identity):
    ctx = make_ctx(identity)
    with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}) as scope:
        return scope


def _predicate(identity):
    ctx = make_ctx(identity)
    with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}) as scope:
        return scope.person_predicate("p.person_id")


def test_self_scope_is_only_the_callers_own_person():
    sql, binds = _predicate(ident("hcm", hr_scope="self"))
    assert sql == "p.person_id = :sec_person_id"
    assert binds == {"sec_person_id": 7}


def test_reports_scope_walks_the_supervisor_hierarchy_from_the_callers_person():
    sql, binds = _predicate(ident("hcm", hr_scope="reports"))
    validate_sql_conventions("SELECT 1 FROM APPS.PER_ALL_PEOPLE_F p WHERE " + sql)
    assert "START WITH paaf.supervisor_id = :sec_person_id" in sql
    assert "NOCYCLE" in sql and "LEVEL <= 15" in sql
    assert "paaf.primary_flag = 'Y'" in sql and "assignment_type = 'E'" in sql
    assert binds == {"sec_person_id": 7}


def test_person_id_is_never_interpolated_into_sql():
    sql, _ = _predicate(ident("hcm", person_id=424242, hr_scope="reports"))
    assert "424242" not in sql


def test_all_scope_is_unrestricted_only_by_explicit_grant():
    sql, binds = _predicate(ident("hcm", hr_scope="all", person_id=None))
    assert sql == "1 = 1" and binds == {}


def test_unmapped_person_is_denied_not_unrestricted():
    ctx = make_ctx(ident("hcm", person_id=None, hr_scope="reports"))
    with pytest.raises(ToolError, match="not tied to an HR person"):
        with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}) as scope:
            scope.person_predicate("p.person_id")


def test_unknown_hr_scope_is_denied():
    ctx = make_ctx(ident("hcm", hr_scope="everyone"))
    with pytest.raises(ToolError, match="Unknown hr_scope"):
        with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}) as scope:
            scope.person_predicate("p.person_id")


# ── field scope ──────────────────────────────────────────────────────────────

FIELDS = [
    FieldSpec("p.full_name", "full_name"),
    FieldSpec("pay.salary", "salary", "compensation"),
    FieldSpec("p.national_identifier", "national_id", "national_id"),
]


def _select(identity, person_column="p.person_id"):
    ctx = make_ctx(identity)
    with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}) as scope:
        return scope.select_fields(FIELDS, person_column)


def test_ungranted_sensitive_fields_are_visible_only_on_the_callers_own_row():
    sql, binds, withheld = _select(ident("hcm"))
    assert "p.full_name AS full_name" in sql
    assert "CASE WHEN p.person_id = :sec_person_id THEN pay.salary END AS salary" in sql
    assert "CASE WHEN p.person_id = :sec_person_id THEN p.national_identifier END" in sql
    assert binds == {"sec_person_id": 7}
    assert {w["field_class"] for w in withheld} == {"compensation", "national_id"}
    assert all(w["withheld_from"] == "records other than your own" for w in withheld)


def test_granted_class_is_selected_plainly_and_others_stay_gated():
    sql, _, withheld = _select(ident("hcm", grants=("compensation",)))
    assert "pay.salary AS salary" in sql
    assert "CASE WHEN" in sql and "national_identifier END" in sql
    assert [w["field_class"] for w in withheld] == ["national_id"]


def test_without_a_person_column_ungranted_fields_are_never_read():
    sql, binds, withheld = _select(ident("hcm"), person_column=None)
    assert "NULL AS salary" in sql and "NULL AS national_id" in sql
    assert "pay.salary" not in sql and "national_identifier" not in sql
    assert binds == {}
    assert all(w["withheld_from"] == "all records" for w in withheld)


def test_person_without_a_person_id_gets_no_own_record_exception():
    sql, _, _ = _select(ident("hcm", person_id=None))
    assert "NULL AS salary" in sql


def test_grant_of_a_different_class_does_not_unlock_another():
    sql, _, _ = _select(ident("hcm", grants=("bank_account",)))
    assert "NULL AS" not in sql  # own-record CASE, not a plain read
    assert "pay.salary AS salary" not in sql


def test_disclosure_names_what_is_withheld():
    ctx = make_ctx(ident("hcm", grants=("tax_id",)))
    with resolve_sensitive_call(ctx, tool_name="t", domain="hcm", params={}) as scope:
        _, _, withheld = scope.select_fields(FIELDS, "p.person_id")
        d = scope.disclosure(withheld)
    assert d["domain"] == "hcm" and d["person_scope"] == "reports"
    assert d["granted_field_classes"] == ["tax_id"]
    assert {w["field"] for w in d["withheld_fields"]} == {"salary", "national_id"}


def test_finance_disclosure_has_no_person_scope():
    ctx = make_ctx(ident("finance"))
    with resolve_sensitive_call(ctx, tool_name="t", domain="finance", params={}) as scope:
        assert scope.disclosure([])["person_scope"] is None


def test_field_classes_match_the_schema_constants():
    import importlib.util

    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "identity_models", root / "identity-service" / "db" / "models.py"
    )
    if spec is None or spec.loader is None or not spec.origin or not pathlib.Path(spec.origin).exists():
        pytest.skip("identity-service not alongside mcp-server")
    models = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(models)
    assert tuple(models.VALID_FIELD_CLASSES) == FIELD_CLASSES


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_record_discloses_released_and_withheld_classes():
    audit = CaptureAudit()
    ctx = make_ctx(ident("hcm", grants=("compensation",)), audit=audit)
    with resolve_sensitive_call(
        ctx, tool_name="employee_profile", domain="hcm", params={"name": "<redacted>"}
    ) as scope:
        scope.person_predicate("p.person_id")
        scope.select_fields(FIELDS, "p.person_id")

    (record,) = audit.records
    assert record.status == "ok"
    sec = record.params["_security"]
    assert sec["domain"] == "hcm" and sec["person_scope"] == "reports"
    assert sec["sensitive_released"] == ["compensation"]
    assert sec["sensitive_withheld"] == ["national_id"]


def test_denied_sensitive_call_is_still_audited():
    audit = CaptureAudit()
    ctx = make_ctx(ident("scm"), audit=audit)
    with pytest.raises(ToolError):
        with resolve_sensitive_call(ctx, tool_name="employee_profile", domain="hcm", params={}):
            pass
    assert audit.records[0].status == "no_identity_mapping"


def test_audit_safe_redacts_only_supplied_personal_values():
    out = audit_safe({"name": "Jane Doe", "ledger": "US", "ssn": None}, ["name", "ssn"])
    assert out == {"name": "<redacted>", "ledger": "US", "ssn": None}


# ── end to end through MCP ───────────────────────────────────────────────────


def _hr_toolset(rows):
    def register(app: MCPServer, ctx: ToolContext) -> None:
        @app.tool()
        def employee_profile(search_name: str | None = None) -> dict:
            """Test-only HR tool built on the framework."""
            params = audit_safe({"search_name": search_name}, ["search_name"])
            with resolve_sensitive_call(
                ctx, tool_name="employee_profile", domain="hcm", params=params
            ) as scope:
                where, binds = scope.person_predicate("p.person_id")
                select, fbinds, withheld = scope.select_fields(FIELDS, "p.person_id")
                sql = (
                    f"SELECT {select} FROM APPS.PER_ALL_PEOPLE_F p "
                    f"LEFT JOIN APPS.PER_PAY_PROPOSALS pay ON pay.assignment_id = p.person_id "
                    f"WHERE {where}"
                )
                result = scope.connector.run(sql, {**binds, **fbinds})
                return {"rows": result, **scope.disclosure(withheld)}

    return ToolSet(name="test_hr", register=register)


async def _call(app, name, args):
    async with InMemoryTransport(app)._connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)


@pytest.mark.asyncio
async def test_framework_tool_end_to_end():
    conn = Recording([{"full_name": "A", "salary": None, "national_id": None}])
    audit = CaptureAudit()
    ctx = make_ctx(ident("hcm"), audit=audit, connector=conn)
    app = MCPServer("t")
    mount_toolsets(app, ctx, [_hr_toolset(conn.rows)])

    result = await _call(app, "employee_profile", {"search_name": "Jane Doe"})
    assert not result.is_error, result.content[0].text
    body = json.loads(result.content[0].text)
    assert {w["field"] for w in body["withheld_fields"]} == {"salary", "national_id"}
    sql, binds = conn.calls[0]
    assert binds == {"sec_person_id": 7}
    assert "Jane Doe" not in json.dumps(audit.records[0].params)
    assert audit.records[0].params["_security"]["sensitive_withheld"] == [
        "compensation", "national_id"
    ]


@pytest.mark.asyncio
async def test_framework_tool_denies_a_non_hcm_caller_over_the_wire():
    ctx = make_ctx(ident("manufacturing"))
    app = MCPServer("t")
    mount_toolsets(app, ctx, [_hr_toolset([])])
    result = await _call(app, "employee_profile", {})
    assert result.is_error
    assert "No identity mapping" in result.content[0].text


# ── guardrails against bypass ────────────────────────────────────────────────

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "src" / "ebsmcp" / "tools"


@pytest.mark.parametrize("family", ["hcm", "finance"])
def test_sensitive_families_never_call_the_plain_resolvers(family):
    offenders = []
    for path in (TOOLS / family).rglob("*.py"):
        text = path.read_text()
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        code = re.sub(r"#.*", "", code)
        if re.search(r"\bresolve_scoped_call\b|\bresolve_identity_only\b|run_scoped_list", code):
            offenders.append(path.name)
    assert not offenders, (
        f"tools/{family} must go through tools.sensitive.resolve_sensitive_call: {offenders}"
    )


def test_non_sensitive_families_never_name_a_sensitive_domain():
    for family in ("scm", "manufacturing", "dba"):
        for path in (TOOLS / family).rglob("*.py"):
            code = re.sub(r'""".*?"""', "", path.read_text(), flags=re.S)
            assert not re.search(r"""domains\s*=\s*\(?\s*["'](hcm|finance)""", code), path


def test_every_functional_tool_declares_its_domains():
    """A tool that omits domains still works (non-sensitive mappings only)
    but cannot be told apart from one that forgot; every scm/manufacturing
    call site must name its family explicitly."""
    for family, const in (("scm", "SCM_DOMAINS"), ("manufacturing", "MANUFACTURING_DOMAINS")):
        for path in (TOOLS / family).glob("*.py"):
            if path.name in ("__init__.py", "_scope.py"):
                continue
            text = path.read_text()
            calls = len(re.findall(r"resolve_scoped_call\(|run_scoped_list\(", text))
            named = text.count(f"domains={const}")
            assert calls == 0 or named >= 1, path.name
