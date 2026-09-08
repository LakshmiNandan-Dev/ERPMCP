import pytest

from ebsmcp.identity import ResolvedIdentity
from ebsmcp.policy import EntitlementDenied, EntitlementFilter


def make_identity(allowed_org_ids: tuple[str, ...]) -> ResolvedIdentity:
    return ResolvedIdentity(
        subject="jdoe@corp.com",
        environment="test",
        target_system="ebs",
        mapped_role="AP_MANAGER",
        allowed_org_ids=allowed_org_ids,
    )


def test_no_requested_scope_returns_full_allowed_set():
    identity = make_identity(("204", "207"))
    result = EntitlementFilter().scope_org_ids(identity)
    assert result == ("204", "207")


def test_requested_scope_narrows_within_allowed_set():
    identity = make_identity(("204", "207", "301"))
    result = EntitlementFilter().scope_org_ids(identity, requested_org_ids=["207"])
    assert result == ("207",)


def test_requested_scope_cannot_widen_beyond_allowed_set():
    """The core security property: asking for an Org ID outside the
    identity's allowed set silently drops it, never grants it."""
    identity = make_identity(("204",))
    result = EntitlementFilter().scope_org_ids(identity, requested_org_ids=["204", "999"])
    assert result == ("204",)


def test_fully_disjoint_request_is_denied():
    identity = make_identity(("204",))
    with pytest.raises(EntitlementDenied):
        EntitlementFilter().scope_org_ids(identity, requested_org_ids=["999"])


def test_identity_with_no_allowed_orgs_is_denied():
    identity = make_identity(())
    with pytest.raises(EntitlementDenied):
        EntitlementFilter().scope_org_ids(identity)
