"""Local admin sign-in — deliberately not Entra/SSO, see
app.config.Settings.admin_session_secret's comment for why. Two layers:

  - Unit tests against app.auth.local's bcrypt hashing and self-issued
    HS256 session tokens directly.
  - Integration tests against the real FastAPI app, with
    app.deps._settings.admin_session_secret monkeypatched for the
    duration of each test — admin_subject reads it fresh on every call
    (unlike the old Entra verifier, there's no cached singleton built at
    import time to work around here), so this is enough to flip modes
    without reloading the app.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert

import app.deps as deps_module
from app.auth.local import create_session_token, hash_password, verify_password, verify_session_token
from app.main import app
from app.models.identity import admin_accounts

from tests.test_identity_mappings_api import _cleanup, _cleanup_engine

TEST_SECRET = "test-session-secret-not-for-real-use"


# ---------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------


def test_password_round_trips_through_hash_and_verify():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)


def test_wrong_password_is_rejected():
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_malformed_hash_is_rejected_not_trusted():
    assert not verify_password("anything", "not-a-real-bcrypt-hash")


# ---------------------------------------------------------------------
# Session tokens: self-issued HS256, not third-party-verified like Entra
# ---------------------------------------------------------------------


def test_valid_session_token_resolves_to_its_subject():
    token = create_session_token(username="admin@corp.com", secret=TEST_SECRET)
    assert verify_session_token(token, secret=TEST_SECRET) == "admin@corp.com"


def test_session_token_signed_with_a_different_secret_is_rejected():
    token = create_session_token(username="admin@corp.com", secret=TEST_SECRET)
    assert verify_session_token(token, secret="a-completely-different-test-secret") is None


def test_expired_session_token_is_rejected():
    now = int(time.time())
    token = jwt.encode({"sub": "admin@corp.com", "iat": now - 100, "exp": now - 1}, TEST_SECRET, algorithm="HS256")
    assert verify_session_token(token, secret=TEST_SECRET) is None


def test_session_token_missing_required_claims_is_rejected():
    token = jwt.encode({"iat": int(time.time())}, TEST_SECRET, algorithm="HS256")  # no sub, no exp
    assert verify_session_token(token, secret=TEST_SECRET) is None


# ---------------------------------------------------------------------
# Integration: the real app
# ---------------------------------------------------------------------

client = TestClient(app)


def _seed_admin(username: str, password: str, *, is_active: bool = True) -> None:
    with _cleanup_engine.begin() as conn:
        conn.execute(
            insert(admin_accounts).values(
                username=username, password_hash=hash_password(password), is_active=is_active
            )
        )


def _delete_admin(username: str) -> None:
    with _cleanup_engine.begin() as conn:
        conn.execute(delete(admin_accounts).where(admin_accounts.c.username == username))


@pytest.fixture()
def real_auth(monkeypatch):
    # admin_subject reads app.deps._settings (the one Settings instance
    # built at import time) fresh on every request rather than a
    # build-once verifier object, so mutating that instance directly is
    # enough to flip modes without reloading the app.
    monkeypatch.setattr(deps_module._settings, "admin_session_secret", TEST_SECRET)


def test_login_is_503_when_local_auth_is_not_configured():
    resp = client.post("/auth/login", json={"username": "admin@corp.com", "password": "x"})
    assert resp.status_code == 503


def test_login_rejects_unknown_username(real_auth):
    resp = client.post("/auth/login", json={"username": "no-such-admin@corp.com", "password": "x"})
    assert resp.status_code == 401


def test_login_rejects_wrong_password(real_auth):
    _seed_admin("pytest.login@corp.com", "right-password")
    try:
        resp = client.post("/auth/login", json={"username": "pytest.login@corp.com", "password": "wrong-password"})
        assert resp.status_code == 401
    finally:
        _delete_admin("pytest.login@corp.com")


def test_login_rejects_inactive_account(real_auth):
    _seed_admin("pytest.inactive@corp.com", "right-password", is_active=False)
    try:
        resp = client.post(
            "/auth/login", json={"username": "pytest.inactive@corp.com", "password": "right-password"}
        )
        assert resp.status_code == 401
    finally:
        _delete_admin("pytest.inactive@corp.com")


def test_login_succeeds_and_the_token_gates_reads_and_writes(real_auth):
    _seed_admin("pytest.realauth@corp.com", "right-password")
    subject = "pytest.local.auth.mapping@corp.com"
    _cleanup(subject)
    try:
        login_resp = client.post(
            "/auth/login", json={"username": "pytest.realauth@corp.com", "password": "right-password"}
        )
        assert login_resp.status_code == 200
        body = login_resp.json()
        assert body["subject"] == "pytest.realauth@corp.com"
        token = body["access_token"]

        # Reads require it too, same as writes.
        assert client.get("/identity-mappings").status_code == 401
        assert client.get("/identity-mappings", headers={"Authorization": f"Bearer {token}"}).status_code == 200

        # The old stub header can no longer forge identity once real
        # auth is on — created_by follows the token, not the header.
        create_resp = client.post(
            "/identity-mappings",
            headers={"Authorization": f"Bearer {token}", "X-Admin-Subject": "spoofed@corp.com"},
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
        assert create_resp.status_code == 201
        assert create_resp.json()["created_by"] == "pytest.realauth@corp.com"
    finally:
        _cleanup(subject)
        _delete_admin("pytest.realauth@corp.com")


def test_invalid_bearer_token_is_401(real_auth):
    resp = client.get("/identity-mappings", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
