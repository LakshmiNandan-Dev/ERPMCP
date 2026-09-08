"""Requires IDENTITY_DB_URL / AUDIT_DB_URL pointed at real, migrated
databases (see identity-service/audit-service Alembic setups) — these
tests exercise the actual API against a real Postgres instance, the same
one used for manual verification, not a mock.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.main import app
from app.models.identity import identity_mapping_org_scope, identity_mappings

client = TestClient(app)
ADMIN = {"X-Admin-Subject": "admin@corp.com"}
# Reads now require an authenticated caller too, same as writes — see
# app/deps.py's admin_subject. Kept separate from `client` (which stays
# header-free) so test_create_requires_admin_subject_header can still
# exercise the no-header path.
authed_client = TestClient(app, headers=ADMIN)

# Deliberately NOT app.db.identity_engine: that connects as
# mgmt_identity_writer, which has no DELETE on identity_mappings by
# design (see app/db.py) — the same restriction the API itself lives
# under. Test cleanup needs a superuser connection specifically because
# the app's own credentials correctly can't do this; TEST_DB_SUPERUSER_URL
# is test-only, never used by application code.
_cleanup_engine = create_engine(
    os.environ.get(
        "TEST_DB_SUPERUSER_URL", "postgresql+psycopg://ebsmcp:ebsmcp@localhost:5433/ebsmcp_identity"
    )
)


def _cleanup(subject: str) -> None:
    with _cleanup_engine.begin() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                identity_mappings.select().where(identity_mappings.c.entra_subject == subject)
            )
        ]
        for mid in ids:
            conn.execute(
                identity_mapping_org_scope.delete().where(
                    identity_mapping_org_scope.c.identity_mapping_id == mid
                )
            )
        conn.execute(identity_mappings.delete().where(identity_mappings.c.entra_subject == subject))


def test_create_requires_admin_subject_header():
    resp = client.post(
        "/identity-mappings",
        json={
            "entra_subject": "no-header@corp.com",
            "environment": "dev",
            "target_system": "ebs",
            "target_username": "JDOE",
            "mapped_role": "X",
            "effective_start_date": "2026-01-01T00:00:00Z",
            "org_scope": [{"org_id": "1"}],
        },
    )
    assert resp.status_code == 400


def test_create_list_conflict_close_and_role_change_flow():
    subject = "pytest.user@corp.com"
    _cleanup(subject)
    try:
        create_resp = client.post(
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
                "org_scope": [{"org_id": "204", "resolved_from_source": True}],
            },
        )
        assert create_resp.status_code == 201
        mapping_id = create_resp.json()["id"]
        assert create_resp.json()["resolution_source"] == "resolved_from_source"

        listed = authed_client.get("/identity-mappings", params={"entra_subject": subject})
        assert [m["id"] for m in listed.json()] == [mapping_id]

        conflict_resp = client.post(
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
        assert conflict_resp.status_code == 409

        close_resp = client.post(f"/identity-mappings/{mapping_id}/close", headers=ADMIN)
        assert close_resp.status_code == 200
        assert close_resp.json()["effective_end_date"] is not None

        double_close_resp = client.post(f"/identity-mappings/{mapping_id}/close", headers=ADMIN)
        assert double_close_resp.status_code == 404

        role_change_resp = client.post(
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
        assert role_change_resp.status_code == 201

        open_only = authed_client.get("/identity-mappings", params={"entra_subject": subject})
        assert [m["mapped_role"] for m in open_only.json()] == ["AP_SUPERVISOR"]
    finally:
        _cleanup(subject)


def test_manually_overridden_resolution_source_when_any_scope_row_is_manual():
    subject = "pytest.manual@corp.com"
    _cleanup(subject)
    try:
        resp = client.post(
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
                "org_scope": [
                    {"org_id": "204", "resolved_from_source": True},
                    {"org_id": "999", "resolved_from_source": False},
                ],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["resolution_source"] == "manually_overridden"
    finally:
        _cleanup(subject)


def test_audit_log_endpoint_is_reachable_and_read_only_by_construction():
    resp = authed_client.get("/audit-log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
