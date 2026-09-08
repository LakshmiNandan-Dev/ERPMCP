"""Direct unit tests of resolve_scoped_call's ebs_dba branch — no
MCPServer/ClientSession needed, since it's a plain @contextmanager
function callable on its own (current_subject falls back to
ctx.dev_subject whenever get_access_token() returns None, exactly as it
does for every call made outside a live streamable-http request).

test_instance_health_tool.py is the full wire-protocol proof (matching
test_example_health_tool.py's own stated rationale); this file exists to
prove the narrower claim that the new ebs_dba branch doesn't regress the
existing org-scoped path, without dragging MCPServer/ClientSession/
InMemoryTransport into what's really a two-branch conditional.
"""

from __future__ import annotations

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors import MockEBSConnector
from ebsmcp.connectors.base import EBSConnector
from ebsmcp.identity import ResolvedIdentity, StubIdentityResolver
from ebsmcp.policy import EntitlementDenied, EntitlementFilter
from ebsmcp.tools import ToolContext, permitted_instances, resolve_identity_only, resolve_scoped_call
from mcp.server.mcpserver.exceptions import ToolError

import pytest

SUBJECT = "jdoe@corp.com"


def build_ctx(
    *mappings: ResolvedIdentity, connectors: dict[str, EBSConnector] | None = None
) -> ToolContext:
    return ToolContext(
        connectors=connectors if connectors is not None else {"TEST": MockEBSConnector()},
        identity_resolver=StubIdentityResolver(*mappings),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment="test",
        dev_subject=SUBJECT,
    )


def test_ebs_dba_call_skips_org_scoping_and_yields_empty_effective_org_ids():
    ctx = build_ctx(
        ResolvedIdentity(
            subject=SUBJECT,
            environment="test",
            target_system="ebs_dba",
            mapped_role="Senior DBA — patching access",
            allowed_org_ids=(),
        )
    )

    with resolve_scoped_call(ctx, tool_name="t", target_system="ebs_dba", params={}) as (
        identity,
        effective_org_ids,
        connector,
    ):
        assert identity.target_system == "ebs_dba"
        assert effective_org_ids == ()
        assert connector is ctx.connectors["TEST"]


def test_ebs_call_with_no_allowed_orgs_still_raises_entitlement_denied():
    """Proves the new ebs_dba branch didn't accidentally widen the
    existing org-scoped path — an ebs identity with no allowed orgs must
    still be denied, exactly as it was before this change."""
    ctx = build_ctx(
        ResolvedIdentity(
            subject=SUBJECT,
            environment="test",
            target_system="ebs",
            mapped_role="AP_MANAGER",
            allowed_org_ids=(),
        )
    )

    with pytest.raises(EntitlementDenied):
        with resolve_scoped_call(ctx, tool_name="t", target_system="ebs", params={}):
            pass


def _identity(**overrides) -> ResolvedIdentity:
    defaults = dict(
        subject=SUBJECT,
        environment="test",
        target_system="ebs_dba",
        mapped_role="Senior DBA — patching access",
        allowed_org_ids=(),
    )
    defaults.update(overrides)
    return ResolvedIdentity(**defaults)


def test_single_configured_instance_is_auto_selected_with_no_param():
    ctx = build_ctx(_identity(), connectors={"PROD": MockEBSConnector()})

    with resolve_scoped_call(ctx, tool_name="t", target_system="ebs_dba", params={}) as (
        _identity_,
        _org_ids,
        connector,
    ):
        assert connector is ctx.connectors["PROD"]


def test_multiple_instances_with_no_param_raises_tool_error_listing_options():
    ctx = build_ctx(
        _identity(),
        connectors={"PROD": MockEBSConnector(), "UAT": MockEBSConnector()},
    )

    with pytest.raises(ToolError, match="PROD, UAT"):
        with resolve_scoped_call(ctx, tool_name="t", target_system="ebs_dba", params={}):
            pass


def test_explicit_instance_param_selects_that_connector_case_insensitively():
    prod, uat = MockEBSConnector(), MockEBSConnector()
    ctx = build_ctx(_identity(), connectors={"PROD": prod, "UAT": uat})

    with resolve_scoped_call(
        ctx, tool_name="t", target_system="ebs_dba", params={}, requested_instance="uat"
    ) as (_identity_, _org_ids, connector):
        assert connector is uat


def test_unknown_instance_name_is_rejected():
    ctx = build_ctx(_identity(), connectors={"PROD": MockEBSConnector()})

    with pytest.raises(ToolError, match="Unknown EBS instance"):
        with resolve_scoped_call(
            ctx, tool_name="t", target_system="ebs_dba", params={}, requested_instance="QA"
        ):
            pass


def test_caller_outside_their_allowed_instances_is_denied():
    ctx = build_ctx(
        _identity(allowed_instances=("UAT",)),
        connectors={"PROD": MockEBSConnector(), "UAT": MockEBSConnector()},
    )

    with pytest.raises(ToolError, match="not permitted"):
        with resolve_scoped_call(
            ctx, tool_name="t", target_system="ebs_dba", params={}, requested_instance="PROD"
        ):
            pass


def test_caller_with_no_allowed_instances_overlap_and_no_param_is_denied():
    """allowed_instances=('UAT',) but only PROD is configured in this
    deployment — no permitted instance exists here at all, distinct from
    the "ambiguous, pick one" case."""
    ctx = build_ctx(
        _identity(allowed_instances=("UAT",)),
        connectors={"PROD": MockEBSConnector()},
    )

    with pytest.raises(ToolError, match="no permitted EBS instance"):
        with resolve_scoped_call(ctx, tool_name="t", target_system="ebs_dba", params={}):
            pass


def test_none_allowed_instances_is_unrestricted_default():
    """The default (no instance scoping ever recorded) must not lock a
    caller out of every instance the moment allowed_instances exists as a
    field — see ResolvedIdentity's docstring."""
    ctx = build_ctx(
        _identity(allowed_instances=None),
        connectors={"PROD": MockEBSConnector(), "UAT": MockEBSConnector()},
    )

    with resolve_scoped_call(
        ctx, tool_name="t", target_system="ebs_dba", params={}, requested_instance="PROD"
    ) as (_identity_, _org_ids, connector):
        assert connector is ctx.connectors["PROD"]


def test_empty_tuple_allowed_instances_denies_every_instance():
    ctx = build_ctx(
        _identity(allowed_instances=()),
        connectors={"PROD": MockEBSConnector()},
    )

    with pytest.raises(ToolError, match="no permitted EBS instance"):
        with resolve_scoped_call(ctx, tool_name="t", target_system="ebs_dba", params={}):
            pass


def test_resolve_identity_only_never_forces_a_single_instance():
    """The whole point of this helper: unlike resolve_scoped_call, it must
    not fail just because more than one instance is configured — that's
    exactly the ambiguity list_ebs_instances exists to resolve for the
    caller, not something this helper should short-circuit on."""
    ctx = build_ctx(
        _identity(),
        connectors={"PROD": MockEBSConnector(), "UAT": MockEBSConnector(), "QA": MockEBSConnector()},
    )

    with resolve_identity_only(
        ctx, tool_name="list_ebs_instances", target_system="ebs_dba", params={}
    ) as identity:
        assert identity.subject == SUBJECT


def test_resolve_identity_only_still_denies_an_unmapped_caller():
    ctx = build_ctx(connectors={"PROD": MockEBSConnector()})

    with pytest.raises(ToolError, match="No identity mapping"):
        with resolve_identity_only(
            ctx, tool_name="list_ebs_instances", target_system="ebs_dba", params={}
        ):
            pass


def test_permitted_instances_intersects_configured_and_allowed():
    ctx = build_ctx(
        connectors={"PROD": MockEBSConnector(), "UAT": MockEBSConnector(), "QA": MockEBSConnector()}
    )

    assert permitted_instances(ctx, _identity(allowed_instances=None)) == {"PROD", "UAT", "QA"}
    assert permitted_instances(ctx, _identity(allowed_instances=("UAT", "QA"))) == {"UAT", "QA"}
    assert permitted_instances(ctx, _identity(allowed_instances=())) == set()
