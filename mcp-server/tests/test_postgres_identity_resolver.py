"""Exercises PostgresIdentityResolver's actual SQL against a real (SQLite,
in-memory) database rather than a mock — the queries here are plain
SELECT/WHERE/IS NULL with no dialect-specific constructs, so SQLite gives
genuine execution-based confidence without needing Docker for this one.
The class is still named for Postgres because that's the real deployment
target (and Oracle, per the pluggable design) — this test just proves the
query logic itself is correct. engine and seed_mapping are shared fixtures
in conftest.py — test_streamable_http_auth.py reuses them too.
"""

from __future__ import annotations

import pytest

from ebsmcp.identity import PostgresIdentityResolver


@pytest.fixture()
def resolver(engine):
    return PostgresIdentityResolver(db_url="unused", environment="prod", engine=engine)


def test_resolves_mapped_subject_with_org_scope(resolver, seed_mapping):
    seed_mapping(
        subject="jdoe@corp.com", environment="prod", target_system="ebs",
        mapped_role="AP_MANAGER", org_ids=["204", "207"],
    )
    identity = resolver.resolve("jdoe@corp.com", "ebs")
    assert identity.mapped_role == "AP_MANAGER"
    assert set(identity.allowed_org_ids) == {"204", "207"}
    assert identity.environment == "prod"


def test_unmapped_subject_raises_lookup_error(resolver):
    with pytest.raises(LookupError):
        resolver.resolve("nobody@corp.com", "ebs")


def test_closed_mapping_is_not_resolved(resolver, seed_mapping):
    """The same invariant proven at the schema layer, now proven from the
    resolver's own read path: a closed-out mapping must not grant access,
    even though the row still exists for audit history.
    """
    seed_mapping(
        subject="jdoe@corp.com", environment="prod", target_system="ebs",
        mapped_role="AP_MANAGER", org_ids=["204"], closed=True,
    )
    with pytest.raises(LookupError):
        resolver.resolve("jdoe@corp.com", "ebs")


def test_mapping_for_a_different_target_system_does_not_leak_across(resolver, seed_mapping):
    seed_mapping(
        subject="jdoe@corp.com", environment="prod", target_system="fusion",
        mapped_role="AP Invoice Reviewer", org_ids=["BU-US1"],
    )
    with pytest.raises(LookupError):
        resolver.resolve("jdoe@corp.com", "ebs")


def test_ebs_dba_mapping_resolves_with_no_org_scope(resolver, seed_mapping):
    """ebs_dba mappings are all-or-nothing — no org_scope rows by design
    (see identity-service/db/models.py) — so allowed_org_ids must resolve
    to an empty tuple, not raise or default to something else."""
    seed_mapping(
        subject="dba@corp.com", environment="prod", target_system="ebs_dba",
        mapped_role="Senior DBA — patching access", org_ids=[],
    )
    identity = resolver.resolve("dba@corp.com", "ebs_dba")
    assert identity.allowed_org_ids == ()
    assert identity.mapped_role == "Senior DBA — patching access"


def test_mapping_with_no_instances_seeded_is_unrestricted(resolver, seed_mapping):
    """No instances= kwarg at all — every mapping seeded before this
    concept existed — must resolve to allowed_instances=None (unrestricted),
    never an empty tuple (deny all)."""
    seed_mapping(
        subject="dba@corp.com", environment="prod", target_system="ebs_dba",
        mapped_role="Senior DBA — patching access", org_ids=[],
    )
    identity = resolver.resolve("dba@corp.com", "ebs_dba")
    assert identity.allowed_instances is None


def test_mapping_restricted_to_specific_instances(resolver, seed_mapping):
    seed_mapping(
        subject="dba@corp.com", environment="prod", target_system="ebs_dba",
        mapped_role="Senior DBA — patching access", org_ids=[],
        instances=["PROD", "UAT"],
    )
    identity = resolver.resolve("dba@corp.com", "ebs_dba")
    assert set(identity.allowed_instances) == {"PROD", "UAT"}


def test_mapping_explicitly_restricted_to_zero_instances(resolver, seed_mapping):
    """instances=[] (as opposed to not passing instances at all) is a real,
    explicit "no EBS instance access" grant — must resolve to an empty
    tuple, not fall back to unrestricted."""
    seed_mapping(
        subject="dba@corp.com", environment="prod", target_system="ebs_dba",
        mapped_role="Senior DBA — patching access", org_ids=[],
        instances=[],
    )
    identity = resolver.resolve("dba@corp.com", "ebs_dba")
    assert identity.allowed_instances == ()


# ── domain selection, person scope and sensitive grants ──────────────────────


def _two_mappings(seed_mapping):
    seed_mapping(
        subject="jdoe@corp.com", environment="prod", target_system="ebs",
        mapped_role="AP Manager", org_ids=["204"], domain="finance",
    )
    seed_mapping(
        subject="jdoe@corp.com", environment="prod", target_system="ebs",
        mapped_role="Inventory Manager", org_ids=["300", "301"], domain="scm",
    )


def test_mapping_choice_follows_the_requested_domain_not_row_order(resolver, seed_mapping):
    """The old .first() returned whichever row the database happened to
    yield, so the same caller got a different Org scope depending on it."""
    _two_mappings(seed_mapping)
    scm = resolver.resolve("jdoe@corp.com", "ebs", ("scm",))
    assert scm.domain == "scm" and set(scm.allowed_org_ids) == {"300", "301"}
    fin = resolver.resolve("jdoe@corp.com", "ebs", ("finance",))
    assert fin.domain == "finance" and fin.allowed_org_ids == ("204",)


def test_no_domain_requested_never_returns_a_sensitive_mapping(resolver, seed_mapping):
    _two_mappings(seed_mapping)
    identity = resolver.resolve("jdoe@corp.com", "ebs")
    assert identity.domain == "scm"


def test_user_with_only_a_sensitive_mapping_is_denied_when_no_domain_requested(
    resolver, seed_mapping
):
    seed_mapping(
        subject="hr@corp.com", environment="prod", target_system="ebs",
        mapped_role="HR Manager", org_ids=["204"], domain="hcm", person_id=77,
    )
    with pytest.raises(LookupError):
        resolver.resolve("hr@corp.com", "ebs")
    assert resolver.resolve("hr@corp.com", "ebs", ("hcm",)).person_id == 77


def test_requesting_a_domain_the_user_lacks_is_denial_not_fallback(resolver, seed_mapping):
    _two_mappings(seed_mapping)
    with pytest.raises(LookupError, match="hcm"):
        resolver.resolve("jdoe@corp.com", "ebs", ("hcm",))


def test_person_scope_and_grants_are_resolved(resolver, seed_mapping):
    seed_mapping(
        subject="hr@corp.com", environment="prod", target_system="ebs",
        mapped_role="HR Specialist", org_ids=["204"], domain="hcm",
        person_id=1234, hr_scope="all", grants=["compensation", "tax_id"],
    )
    identity = resolver.resolve("hr@corp.com", "ebs", ("hcm",))
    assert identity.person_id == 1234
    assert identity.hr_scope == "all"
    assert identity.sensitive_grants == frozenset({"compensation", "tax_id"})


def test_mapping_with_no_grants_and_default_scope_is_narrow(resolver, seed_mapping):
    seed_mapping(
        subject="mgr@corp.com", environment="prod", target_system="ebs",
        mapped_role="Manager", org_ids=["204"], domain="hcm", person_id=9,
    )
    identity = resolver.resolve("mgr@corp.com", "ebs", ("hcm",))
    assert identity.sensitive_grants == frozenset()
    assert identity.hr_scope == "reports"
