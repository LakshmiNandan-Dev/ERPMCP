from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models.identity import entra_registrations

from tests.test_identity_mappings_api import _cleanup_engine

client = TestClient(app)
ADMIN = {"X-Admin-Subject": "admin@corp.com"}
# GET /entra-config now requires an authenticated caller too, same as
# every other admin-gui-facing endpoint — see app/deps.py's admin_subject.
authed_client = TestClient(app, headers=ADMIN)


def _cleanup_entra_config(environment: str) -> None:
    with _cleanup_engine.begin() as conn:
        conn.execute(entra_registrations.delete().where(entra_registrations.c.environment == environment))


def test_get_before_any_config_is_404():
    _cleanup_entra_config("test")
    resp = authed_client.get("/entra-config/test")
    assert resp.status_code == 404


def test_put_requires_admin_subject_header():
    resp = client.put(
        "/entra-config/test",
        json={"tenant_id": "t", "audience": "a", "resource_server_url": "https://x/"},
    )
    assert resp.status_code == 400


def test_create_then_update_sets_updated_fields_correctly():
    _cleanup_entra_config("test")
    try:
        created = client.put(
            "/entra-config/test",
            headers=ADMIN,
            json={"tenant_id": "tenant-1", "audience": "aud-1", "resource_server_url": "https://x/"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["created_by"] == "admin@corp.com"
        assert body["updated_at"] is None
        assert body["subject_claim"] == "preferred_username"

        updated = client.put(
            "/entra-config/test",
            headers={"X-Admin-Subject": "admin2@corp.com"},
            json={
                "tenant_id": "tenant-2",
                "audience": "aud-1",
                "subject_claim": "oid",
                "resource_server_url": "https://x/",
            },
        )
        assert updated.status_code == 200
        updated_body = updated.json()
        assert updated_body["id"] == body["id"]  # same row, upserted, not a duplicate
        assert updated_body["tenant_id"] == "tenant-2"
        assert updated_body["subject_claim"] == "oid"
        assert updated_body["created_by"] == "admin@corp.com"  # unchanged
        assert updated_body["updated_by"] == "admin2@corp.com"
        assert updated_body["updated_at"] is not None  # the bug this caught

        fetched = authed_client.get("/entra-config/test")
        assert fetched.json() == updated_body
    finally:
        _cleanup_entra_config("test")


def test_config_is_scoped_per_environment():
    _cleanup_entra_config("uat")
    try:
        client.put(
            "/entra-config/uat",
            headers=ADMIN,
            json={"tenant_id": "uat-tenant", "audience": "a", "resource_server_url": "https://x/"},
        )
        assert authed_client.get("/entra-config/dev").status_code == 404
        assert authed_client.get("/entra-config/uat").status_code == 200
    finally:
        _cleanup_entra_config("uat")
