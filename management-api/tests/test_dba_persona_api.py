"""ebs_dba is a persona, not a third backend — DBA tool access isn't
Org-scoped, doesn't need an FND_USER account the way ebs/fusion mappings
do, and is all-or-nothing: a mapping grants the full DBA toolset, not a
per-category subset. See identity-service/db/models.py's
VALID_TARGET_SYSTEMS comment and app/schemas/identity.py's
model_validator for the rules this covers.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

from tests.test_identity_mappings_api import _cleanup

client = TestClient(app)
ADMIN = {"X-Admin-Subject": "admin@corp.com"}


def test_dba_mapping_needs_no_org_scope_and_grants_full_toolset_access():
    subject = "pytest.dba@corp.com"
    _cleanup(subject)
    try:
        resp = client.post(
            "/identity-mappings",
            headers=ADMIN,
            json={
                "entra_subject": subject,
                "environment": "dev",
                "target_system": "ebs_dba",
                "mapped_role": "Senior DBA",
                "effective_start_date": "2026-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_username"] is None
        assert body["org_scope"] == []
        # A DBA grant is always a direct admin decision, never something
        # resolved from a source system — this should be
        # manually_overridden every time, not dependent on org_scope
        # (which is empty here and must not be read as "vacuously true").
        assert body["resolution_source"] == "manually_overridden"
    finally:
        _cleanup(subject)


def test_dba_mapping_rejects_any_org_scope():
    resp = client.post(
        "/identity-mappings",
        headers=ADMIN,
        json={
            "entra_subject": "pytest.dba.scoped@corp.com",
            "environment": "dev",
            "target_system": "ebs_dba",
            "mapped_role": "Junior DBA",
            "effective_start_date": "2026-01-01T00:00:00Z",
            "org_scope": [{"org_id": "concurrent_processing"}],
        },
    )
    assert resp.status_code == 422
    assert "org_scope doesn't apply" in resp.text


def test_functional_mapping_still_requires_target_username_and_org_scope():
    resp = client.post(
        "/identity-mappings",
        headers=ADMIN,
        json={
            "entra_subject": "pytest.no.username@corp.com",
            "environment": "dev",
            "target_system": "ebs",
            "mapped_role": "AP_MANAGER",
            "effective_start_date": "2026-01-01T00:00:00Z",
            "org_scope": [{"org_id": "204"}],
        },
    )
    assert resp.status_code == 422
    assert "target_username is required" in resp.text

    resp2 = client.post(
        "/identity-mappings",
        headers=ADMIN,
        json={
            "entra_subject": "pytest.no.orgscope@corp.com",
            "environment": "dev",
            "target_system": "ebs",
            "target_username": "JDOE",
            "mapped_role": "AP_MANAGER",
            "effective_start_date": "2026-01-01T00:00:00Z",
        },
    )
    assert resp2.status_code == 422
    assert "org_scope must include at least one entry" in resp2.text
