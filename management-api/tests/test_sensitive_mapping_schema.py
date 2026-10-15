"""Validation of the hcm/finance security fields on identity mappings. Pure
schema tests — no database — so they run anywhere; the persisted round trip
is covered with the other identity-mapping tests that need real Postgres.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.identity import IdentityMappingCreate


def make(**overrides):
    base = dict(
        entra_subject="jdoe@corp.com", environment="dev", target_system="ebs",
        target_username="JDOE", domain="hcm", mapped_role="HR Manager",
        effective_start_date="2026-01-01T00:00:00Z", org_scope=[{"org_id": "204"}],
        ebs_person_id=1234,
    )
    base.update(overrides)
    return IdentityMappingCreate(**base)


def test_hcm_mapping_defaults_to_reports_and_no_grants():
    m = make()
    assert m.hr_scope == "reports"
    assert m.sensitive_grants == []


def test_hcm_mapping_requires_a_person():
    with pytest.raises(ValidationError, match="ebs_person_id is required"):
        make(ebs_person_id=None)


def test_all_scope_may_omit_the_person():
    assert make(ebs_person_id=None, hr_scope="all").hr_scope == "all"


@pytest.mark.parametrize("domain", ["finance", "scm", "manufacturing", "other"])
def test_person_and_scope_are_rejected_outside_hcm(domain):
    with pytest.raises(ValidationError, match="hcm mappings only"):
        make(domain=domain, ebs_person_id=5)
    with pytest.raises(ValidationError, match="hcm mappings only"):
        make(domain=domain, ebs_person_id=None, hr_scope="self")


@pytest.mark.parametrize("domain", ["hcm", "finance"])
def test_grants_are_accepted_for_hcm_and_finance_and_deduplicated(domain):
    m = make(
        domain=domain,
        ebs_person_id=1234 if domain == "hcm" else None,
        sensitive_grants=["tax_id", "compensation", "tax_id"],
    )
    assert m.sensitive_grants == ["compensation", "tax_id"]


@pytest.mark.parametrize("domain", ["scm", "manufacturing", "other"])
def test_grants_are_rejected_in_non_sensitive_domains(domain):
    with pytest.raises(ValidationError, match="sensitive_grants apply to"):
        make(domain=domain, ebs_person_id=None, sensitive_grants=["compensation"])


def test_unknown_field_class_is_rejected():
    with pytest.raises(ValidationError):
        make(sensitive_grants=["salary"])


def test_dba_mapping_cannot_carry_personal_data_controls():
    with pytest.raises(ValidationError):
        IdentityMappingCreate(
            entra_subject="dba@corp.com", environment="dev", target_system="ebs_dba",
            mapped_role="DBA", effective_start_date="2026-01-01T00:00:00Z",
            sensitive_grants=["compensation"],
        )


def test_person_lookup_resolves_from_the_ebs_user():
    """Connector level, not HTTP: the endpoint sits behind admin auth, which
    needs the identity database. The route itself is a thin 404 wrapper."""
    from app.ebs.lookup import MockEBSLookupConnector

    connector = MockEBSLookupConnector()
    person = connector.find_person("jdoe")
    assert (person.person_id, person.full_name) == (1234, "John Doe")
    assert connector.find_person("NOBODY") is None
