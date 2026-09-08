"""A self-signed RSA keypair standing in for Entra's own signing key, so
EntraTokenVerifier's actual crypto (signature verification against a JWKS,
not just claim parsing) is exercised for real in tests — without needing a
live Entra tenant. issue_token() mints tokens shaped like real Entra ID
tokens (iss, aud, exp, preferred_username, azp, scp).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from ebsmcp.identity.tables import (
    identity_mapping_instance_scope,
    identity_mapping_org_scope,
    identity_mappings,
    metadata,
)

TEST_ISSUER = "https://login.microsoftonline.com/00000000-test-tenant/v2.0"
TEST_AUDIENCE = "11111111-test-client-id"
TEST_KID = "test-key-1"


@pytest.fixture(scope="session")
def test_issuer() -> str:
    return TEST_ISSUER


@pytest.fixture(scope="session")
def test_audience() -> str:
    return TEST_AUDIENCE


@pytest.fixture(scope="session")
def test_kid() -> str:
    return TEST_KID


@pytest.fixture(scope="session")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="session")
def test_jwks(rsa_keypair) -> dict:
    _, public_key = rsa_keypair
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(public_key, as_dict=True)
    jwk["kid"] = TEST_KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


@pytest.fixture()
def issue_token(rsa_keypair):
    private_key, _ = rsa_keypair

    def _issue(
        *,
        subject: str = "jdoe@corp.com",
        issuer: str = TEST_ISSUER,
        audience: str = TEST_AUDIENCE,
        expires_in: int = 3600,
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + expires_in,
            "preferred_username": subject,
            "azp": "22222222-test-copilot-client",
            "scp": "access_as_user",
            **(extra_claims or {}),
        }
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": TEST_KID})

    return _issue


@pytest.fixture()
def engine():
    """A fresh in-memory SQLite database per test, shaped like
    identity-service's schema — see identity/tables.py for why SQLite is
    an honest stand-in here (plain SELECT/WHERE/IS NULL, no dialect-
    specific constructs).

    StaticPool (one connection, shared and never closed) is required, not
    a style choice: plain sqlite:///:memory: hands out a fresh, empty
    database per connection, keyed by thread under SQLAlchemy's default
    pooling. That's invisible in a synchronous test, but the mcp SDK
    dispatches a sync tool function to a worker thread — a real bug this
    caught the hard way in test_streamable_http_auth.py, where seeding
    data on the test's thread was invisible to the tool call running on
    another. A real network database (Postgres/Oracle) has no such
    per-connection-is-a-separate-database behavior; this is purely a
    SQLite-as-test-double concern.
    """
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(eng)
    return eng


@pytest.fixture()
def seed_mapping(engine):
    def _seed(
        *,
        subject,
        environment,
        target_system,
        mapped_role,
        org_ids,
        closed=False,
        instances=None,
    ):
        with engine.begin() as conn:
            mapping_id = conn.execute(
                insert(identity_mappings).values(
                    entra_subject=subject,
                    environment=environment,
                    target_system=target_system,
                    mapped_role=mapped_role,
                    effective_end_date=datetime(2020, 1, 1, tzinfo=timezone.utc) if closed else None,
                    instance_scope_restricted=instances is not None,
                )
            ).inserted_primary_key[0]
            for org_id in org_ids:
                conn.execute(
                    insert(identity_mapping_org_scope).values(
                        identity_mapping_id=mapping_id, org_id=org_id, resolved_from_source=True
                    )
                )
            for instance_name in instances or ():
                conn.execute(
                    insert(identity_mapping_instance_scope).values(
                        identity_mapping_id=mapping_id, instance_name=instance_name
                    )
                )
        return mapping_id

    return _seed
