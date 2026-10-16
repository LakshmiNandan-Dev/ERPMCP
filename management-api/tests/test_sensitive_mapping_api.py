"""Persisted round trip of the hcm/finance security fields through the real
API and a migrated Postgres: person scope and sensitive grants are stored,
returned, closed with the mapping, and validated before touching the
database. Same prerequisites as test_identity_mappings_api.py.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app

client = TestClient(app, headers={"X-Admin-Subject": "admin@corp.com"})

_cleanup_engine = create_engine(
    os.environ.get(
        "TEST_DB_SUPERUSER_URL", "postgresql+psycopg://ebsmcp:ebsmcp@localhost:5433/ebsmcp_identity"
    )
)

SUBJECT = "sensitive-roundtrip@corp.com"


def _cleanup() -> None:
    with _cleanup_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM identity_mappings WHERE entra_subject = :s"), {"s": SUBJECT}
        )


def _body(**overrides):
    base = {
        "entra_subject": SUBJECT, "environment": "dev", "target_system": "ebs",
        "target_username": "JDOE", "domain": "hcm", "mapped_role": "HR Manager",
        "effective_start_date": "2026-01-01T00:00:00Z",
        "org_scope": [{"org_id": "204", "resolved_from_source": True}],
        "ebs_person_id": 1234,
    }
    base.update(overrides)
    return base


def test_hcm_mapping_round_trips_person_scope_and_grants():
    _cleanup()
    try:
        resp = client.post(
            "/identity-mappings",
            json=_body(hr_scope="reports", sensitive_grants=["tax_id", "compensation"]),
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["ebs_person_id"] == 1234
        assert created["hr_scope"] == "reports"
        assert created["sensitive_grants"] == ["compensation", "tax_id"]

        fetched = client.get(f"/identity-mappings/{created['id']}").json()
        assert fetched["sensitive_grants"] == ["compensation", "tax_id"]
        assert fetched["ebs_person_id"] == 1234

        with _cleanup_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT field_class, granted_by FROM identity_mapping_sensitive_grants "
                    "WHERE identity_mapping_id = :i ORDER BY field_class"
                ),
                {"i": created["id"]},
            ).all()
        assert [r[0] for r in stored] == ["compensation", "tax_id"]
        assert {r[1] for r in stored} == {"admin@corp.com"}
    finally:
        _cleanup()


def test_defaults_are_narrow_when_nothing_is_granted():
    _cleanup()
    try:
        created = client.post("/identity-mappings", json=_body()).json()
        assert created["hr_scope"] == "reports"
        assert created["sensitive_grants"] == []
    finally:
        _cleanup()


def test_hcm_mapping_without_a_person_is_rejected_before_the_database():
    resp = client.post("/identity-mappings", json=_body(ebs_person_id=None))
    assert resp.status_code == 422
    assert "ebs_person_id is required" in resp.text


def test_grants_on_a_non_sensitive_domain_are_rejected():
    resp = client.post(
        "/identity-mappings",
        json=_body(domain="scm", ebs_person_id=None, sensitive_grants=["compensation"]),
    )
    assert resp.status_code == 422


def test_database_rejects_an_unknown_field_class_even_if_the_api_did_not():
    _cleanup()
    try:
        created = client.post("/identity-mappings", json=_body()).json()
        try:
            with _cleanup_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO identity_mapping_sensitive_grants "
                        "(identity_mapping_id, field_class, granted_by) "
                        "VALUES (:i, 'salary', 'x')"
                    ),
                    {"i": created["id"]},
                )
            raised = False
        except Exception:  # noqa: BLE001 - any constraint violation is the point
            raised = True
        assert raised
    finally:
        _cleanup()


def test_grants_are_removed_with_the_mapping():
    _cleanup()
    created = client.post(
        "/identity-mappings", json=_body(sensitive_grants=["bank_account"])
    ).json()
    _cleanup()
    with _cleanup_engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM identity_mapping_sensitive_grants WHERE identity_mapping_id = :i"),
            {"i": created["id"]},
        ).scalar_one()
    assert left == 0
