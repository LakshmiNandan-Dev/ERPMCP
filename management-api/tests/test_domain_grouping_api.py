"""domain groups a functional mapping by EBS pillar (finance, SCM,
manufacturing, HCM, ...) so tool/data access can eventually be scoped by
pillar as well as by Org ID — see identity-service/db/models.py's comment
on the domain column and app/schemas/identity.py's model_validator.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

from tests.test_identity_mappings_api import _cleanup, authed_client

client = TestClient(app)
ADMIN = {"X-Admin-Subject": "admin@corp.com"}


def test_ebs_mapping_requires_a_known_domain():
    resp = client.post(
        "/identity-mappings",
        headers=ADMIN,
        json={
            "entra_subject": "pytest.no.domain@corp.com",
            "environment": "dev",
            "target_system": "ebs",
            "target_username": "JDOE",
            "mapped_role": "AP_MANAGER",
            "effective_start_date": "2026-01-01T00:00:00Z",
            "org_scope": [{"org_id": "204"}],
        },
    )
    assert resp.status_code == 422
    assert "domain is required" in resp.text

    resp2 = client.post(
        "/identity-mappings",
        headers=ADMIN,
        json={
            "entra_subject": "pytest.bad.domain@corp.com",
            "environment": "dev",
            "target_system": "ebs",
            "target_username": "JDOE",
            "domain": "not-a-real-pillar",
            "mapped_role": "AP_MANAGER",
            "effective_start_date": "2026-01-01T00:00:00Z",
            "org_scope": [{"org_id": "204"}],
        },
    )
    assert resp2.status_code == 422
    assert "domain must be one of" in resp2.text


def test_dba_mapping_rejects_a_domain():
    resp = client.post(
        "/identity-mappings",
        headers=ADMIN,
        json={
            "entra_subject": "pytest.dba.domain@corp.com",
            "environment": "dev",
            "target_system": "ebs_dba",
            "domain": "finance",
            "mapped_role": "Senior DBA",
            "effective_start_date": "2026-01-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert "aren't domain-scoped" in resp.text


def test_same_subject_can_hold_open_mappings_in_two_domains_at_once():
    # This is the whole point of making domain part of the uniqueness key
    # in identity-service, not just a descriptive column: one person can
    # be onboarded into more than one pillar in the same environment.
    subject = "pytest.multi.domain@corp.com"
    _cleanup(subject)
    try:
        finance_resp = client.post(
            "/identity-mappings",
            headers=ADMIN,
            json={
                "entra_subject": subject,
                "environment": "dev",
                "target_system": "ebs",
                "target_username": "JDOE",
                "domain": "finance",
                "mapped_role": "AP_MANAGER",
                "effective_start_date": "2026-01-01T00:00:00Z",
                "org_scope": [{"org_id": "204"}],
            },
        )
        assert finance_resp.status_code == 201

        scm_resp = client.post(
            "/identity-mappings",
            headers=ADMIN,
            json={
                "entra_subject": subject,
                "environment": "dev",
                "target_system": "ebs",
                "target_username": "JDOE",
                "domain": "scm",
                "mapped_role": "Purchasing Manager",
                "effective_start_date": "2026-01-01T00:00:00Z",
                "org_scope": [{"org_id": "204"}],
            },
        )
        assert scm_resp.status_code == 201

        # But a second open mapping in the *same* domain still conflicts —
        # domain narrows the uniqueness key, it doesn't remove it.
        dup_finance_resp = client.post(
            "/identity-mappings",
            headers=ADMIN,
            json={
                "entra_subject": subject,
                "environment": "dev",
                "target_system": "ebs",
                "target_username": "JDOE",
                "domain": "finance",
                "mapped_role": "AP_SUPERVISOR",
                "effective_start_date": "2026-06-01T00:00:00Z",
                "org_scope": [{"org_id": "207"}],
            },
        )
        assert dup_finance_resp.status_code == 409

        listed = authed_client.get("/identity-mappings", params={"entra_subject": subject})
        domains = {m["domain"] for m in listed.json()}
        assert domains == {"finance", "scm"}
    finally:
        _cleanup(subject)
