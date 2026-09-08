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
