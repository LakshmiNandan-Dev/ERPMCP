"""The Entra / AD group gate: enforcement off by default, fail-closed when on,
channel and domain limits, the policy store, and the wiring through
resolve_scoped_call (audited denial, narrowed domains, no identity lookup for a
refused caller)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector
from ebsmcp.context import set_caller_channel, set_caller_groups
from ebsmcp.identity import ANY_DOMAIN, ResolvedIdentity, StubIdentityResolver
from ebsmcp.identity.tables import entra_group_access, entra_registrations, metadata
from ebsmcp.policy import (
    EntitlementFilter,
    GroupAccessDenied,
    GroupGate,
    GroupPolicy,
    PostgresGroupPolicySource,
    StaticGroupPolicySource,
)
from ebsmcp.policy.group_gate import Grant, groups_from_claims
from ebsmcp.tools import ToolContext
from ebsmcp.tools.registry import resolve_identity_only, resolve_scoped_call
from ebsmcp.tools.sensitive import resolve_sensitive_call

FIN = "0b3f1c2e-1111-2222-3333-444455556666"
OPS = "9a8b7c6d-aaaa-bbbb-cccc-ddddeeeeffff"


def policy(*grants, enforced=True, claim="groups"):
    return GroupPolicy(enforced, claim, tuple(grants))


def grant(group, channel="chat_agent", domains=("*",)):
    return Grant(group, channel, frozenset(domains))


def gate(*grants, enforced=True, claim="groups"):
    return GroupGate(StaticGroupPolicySource(policy(*grants, enforced=enforced, claim=claim)), "test")


def authorize(g, *, groups=None, claims=None, channel="chat_agent", target="ebs", domains=("scm",)):
    return g.authorize(channel=channel, claims=claims, injected_groups=groups,
                       target_system=target, domains=domains)


# ── enforcement off ──────────────────────────────────────────────────────────


def test_enforcement_off_changes_nothing_even_with_no_groups_at_all():
    g = gate(enforced=False)
    assert authorize(g, groups=None, claims=None, domains=("finance",)) == ("finance",)
    assert authorize(g, groups=frozenset(), domains=None) is None


# ── fail closed ──────────────────────────────────────────────────────────────


def test_unknown_membership_is_denied_with_the_reason():
    with pytest.raises(GroupAccessDenied, match="could not be determined.*no validated token"):
        authorize(gate(grant(FIN)), groups=None, claims=None)


def test_token_groups_overage_is_denied_not_guessed():
    claims = {"_claim_names": {"groups": "src1"}}
    with pytest.raises(GroupAccessDenied, match="groups overage"):
        authorize(gate(grant(FIN, "mcp_direct")), claims=claims, channel="mcp_direct")


def test_a_token_without_the_claim_is_denied_and_says_why():
    with pytest.raises(GroupAccessDenied, match="no 'groups' claim"):
        authorize(gate(grant(FIN, "mcp_direct")), claims={"sub": "x"}, channel="mcp_direct")


def test_a_member_of_no_granted_group_is_denied():
    with pytest.raises(GroupAccessDenied, match="not a member of any Entra group authorised"):
        authorize(gate(grant(FIN)), groups=frozenset({OPS}))


def test_enforcement_with_no_grants_denies_everyone():
    with pytest.raises(GroupAccessDenied):
        authorize(gate(), groups=frozenset({FIN}))


# ── membership, channel, case ────────────────────────────────────────────────


def test_a_member_is_let_in():
    assert authorize(gate(grant(FIN)), groups=frozenset({FIN, OPS})) == ("scm",)


def test_group_ids_match_case_insensitively():
    assert authorize(gate(grant(FIN)), groups=frozenset({FIN.upper().lower()})) == ("scm",)
    set_caller_groups([FIN.upper()])
    from ebsmcp.context import get_injected_groups
    assert get_injected_groups() == frozenset({FIN})
    set_caller_groups(None)


def test_a_grant_for_one_channel_does_not_open_the_other():
    g = gate(grant(FIN, "chat_agent"))
    with pytest.raises(GroupAccessDenied):
        authorize(g, claims={"groups": [FIN]}, channel="mcp_direct")


def test_channel_any_covers_both():
    g = gate(grant(FIN, "any"))
    assert authorize(g, groups=frozenset({FIN}), channel="chat_agent") == ("scm",)
    assert authorize(g, claims={"groups": [FIN]}, channel="mcp_direct") == ("scm",)


def test_token_claim_name_is_configurable():
    g = gate(grant(FIN, "mcp_direct"), claim="roles_groups")
    assert authorize(g, claims={"roles_groups": [FIN]}, channel="mcp_direct") == ("scm",)
    with pytest.raises(GroupAccessDenied):
        authorize(g, claims={"groups": [FIN]}, channel="mcp_direct")


def test_groups_from_claims_shapes():
    assert groups_from_claims({"groups": [FIN.upper(), None, ""]}, "groups").groups == frozenset({FIN})
    assert groups_from_claims(None, "groups").groups is None
    assert groups_from_claims({}, "groups").groups == frozenset()


# ── domain limits ────────────────────────────────────────────────────────────


def test_a_finance_only_group_is_refused_scm_tools():
    g = gate(grant(FIN, domains=("finance",)))
    with pytest.raises(GroupAccessDenied, match="domain"):
        authorize(g, groups=frozenset({FIN}), domains=("scm", "manufacturing", "other"))
    assert authorize(g, groups=frozenset({FIN}), domains=("finance",)) == ("finance",)


def test_domains_are_narrowed_to_what_the_groups_allow():
    g = gate(grant(FIN, domains=("manufacturing",)))
    got = authorize(g, groups=frozenset({FIN}), domains=("scm", "manufacturing", "other"))
    assert got == ("manufacturing",)


def test_union_of_matching_groups_applies():
    g = gate(grant(FIN, domains=("finance",)), grant(OPS, domains=("scm",)))
    assert authorize(g, groups=frozenset({FIN, OPS}), domains=("scm",)) == ("scm",)
    assert authorize(g, groups=frozenset({FIN, OPS}), domains=("finance",)) == ("finance",)


def test_star_allows_every_domain():
    g = gate(grant(FIN, domains=("*",)))
    assert authorize(g, groups=frozenset({FIN}), domains=("hcm",)) == ("hcm",)


def test_dba_tools_need_the_dba_domain():
    g = gate(grant(FIN, domains=("finance",)))
    with pytest.raises(GroupAccessDenied, match="DBA"):
        authorize(g, groups=frozenset({FIN}), target="ebs_dba", domains=None)
    g = gate(grant(FIN, domains=("dba",)))
    assert authorize(g, groups=frozenset({FIN}), target="ebs_dba", domains=None) is None


def test_a_tool_with_no_business_data_needs_membership_only():
    g = gate(grant(FIN, domains=("finance",)))
    assert authorize(g, groups=frozenset({FIN}), domains=ANY_DOMAIN) == ANY_DOMAIN
    assert authorize(g, groups=frozenset({FIN}), domains=None) is None


# ── the policy store ─────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    e = create_engine("sqlite://")
    metadata.create_all(e)
    return e


def seed(e, *, enforce, claim="groups", grants=()):
    with e.begin() as c:
        c.execute(insert(entra_registrations).values(
            environment="test", enforce_group_access=enforce, groups_claim=claim))
        for gid, channel, doms, ended in grants:
            c.execute(insert(entra_group_access).values(
                environment="test", group_object_id=gid, channel=channel,
                allowed_domains=doms, effective_end_date=ended))


def test_store_reads_only_open_grants_and_normalises_ids(engine):
    from datetime import datetime, timezone
    seed(engine, enforce=True, claim="grp", grants=[
        (FIN.upper(), "chat_agent", "finance,dba", None),
        (OPS, "any", "*", datetime(2026, 1, 1, tzinfo=timezone.utc)),   # closed
    ])
    p = PostgresGroupPolicySource("sqlite://", engine=engine).load("test")
    assert p.enforced and p.groups_claim == "grp"
    assert p.grants == (Grant(FIN, "chat_agent", frozenset({"finance", "dba"})),)


def test_store_with_enforcement_off_returns_no_grants(engine):
    seed(engine, enforce=False, grants=[(FIN, "any", "*", None)])
    p = PostgresGroupPolicySource("sqlite://", engine=engine).load("test")
    assert not p.enforced and p.grants == ()


def test_environment_with_no_registration_is_not_enforced(engine):
    p = PostgresGroupPolicySource("sqlite://", engine=engine).load("prod")
    assert not p.enforced


def test_revocation_is_immediate_because_nothing_is_cached(engine):
    from datetime import datetime, timezone
    seed(engine, enforce=True, grants=[(FIN, "chat_agent", "*", None)])
    g = GroupGate(PostgresGroupPolicySource("sqlite://", engine=engine), "test")
    assert authorize(g, groups=frozenset({FIN})) == ("scm",)
    with engine.begin() as c:
        c.execute(entra_group_access.update().values(effective_end_date=datetime.now(timezone.utc)))
    with pytest.raises(GroupAccessDenied):
        authorize(g, groups=frozenset({FIN}))


# ── wiring through the tool entry points ─────────────────────────────────────


class Recording(EBSConnector):
    def execute_query(self, sql, binds=None):
        return []


class CountingResolver(StubIdentityResolver):
    def __init__(self, *a):
        super().__init__(*a)
        self.calls = []

    def resolve(self, subject, target_system, domains=None):
        self.calls.append(domains)
        return super().resolve(subject, target_system, domains)


class CaptureAudit(AuditLogger):
    def __init__(self):
        super().__init__()
        self.records = []

    def _emit(self, record):
        self.records.append(record)


def ctx_for(g, *identities):
    resolver = CountingResolver(*identities)
    audit = CaptureAudit()
    return ToolContext(
        connectors={"T": Recording()}, identity_resolver=resolver, entitlement=EntitlementFilter(),
        audit=audit, environment="test", dev_subject="u@corp.com", group_gate=g,
    ), resolver, audit


def ident(domain, target="ebs"):
    return ResolvedIdentity(subject="u@corp.com", environment="test", target_system=target,
                            mapped_role="R", allowed_org_ids=("204",) if target == "ebs" else (),
                            domain=domain)


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    set_caller_groups(None)
    set_caller_channel(None)


def test_a_refused_caller_is_audited_as_denied_and_never_reaches_the_identity_lookup():
    ctx, resolver, audit = ctx_for(gate(grant(FIN)), ident("scm"))
    set_caller_channel("chat_agent")
    set_caller_groups([OPS])
    with pytest.raises(GroupAccessDenied):
        with resolve_scoped_call(ctx, tool_name="t", target_system="ebs", params={}, domains=("scm",)):
            pass
    assert resolver.calls == []
    assert audit.records[0].status == "denied"


def test_an_allowed_caller_gets_the_narrowed_domains():
    ctx, resolver, _ = ctx_for(gate(grant(FIN, domains=("scm",))), ident("scm"), ident("manufacturing"))
    set_caller_channel("chat_agent")
    set_caller_groups([FIN])
    with resolve_scoped_call(ctx, tool_name="t", target_system="ebs", params={},
                             domains=("manufacturing", "scm", "other")) as (identity, _, _):
        assert identity.domain == "scm"
    assert resolver.calls == [("scm",)]


def test_host_must_declare_the_channel_or_the_default_is_the_stricter_mcp_direct():
    ctx, _, _ = ctx_for(gate(grant(FIN, "chat_agent")), ident("scm"))
    set_caller_groups([FIN])   # channel not set -> treated as mcp_direct
    with pytest.raises(GroupAccessDenied):
        with resolve_scoped_call(ctx, tool_name="t", target_system="ebs", params={}, domains=("scm",)):
            pass


def test_finance_tools_are_held_to_the_finance_domain():
    ctx, _, _ = ctx_for(gate(grant(FIN, domains=("scm",))), ident("finance"))
    set_caller_channel("chat_agent")
    set_caller_groups([FIN])
    with pytest.raises(GroupAccessDenied, match="finance"):
        with resolve_sensitive_call(ctx, tool_name="t", domain="finance", params={}):
            pass


def test_identity_only_tools_also_need_group_membership():
    ctx, _, audit = ctx_for(gate(grant(FIN)), ident("scm"))
    set_caller_channel("chat_agent")
    set_caller_groups([OPS])
    with pytest.raises(GroupAccessDenied):
        with resolve_identity_only(ctx, tool_name="list_ebs_instances", target_system="ebs", params={}):
            pass
    assert audit.records[0].status == "denied"
    set_caller_groups([FIN])
    with resolve_identity_only(ctx, tool_name="list_ebs_instances", target_system="ebs", params={}):
        pass


def test_no_gate_means_nothing_changes():
    ctx, _, _ = ctx_for(None, ident("scm"))
    with resolve_scoped_call(ctx, tool_name="t", target_system="ebs", params={}, domains=("scm",)):
        pass
