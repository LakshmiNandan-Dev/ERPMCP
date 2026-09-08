import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ebsmcp.auth import EntraTokenVerifier, StaticJWKSSource


@pytest.fixture()
def verifier(test_jwks, test_issuer, test_audience):
    return EntraTokenVerifier(
        jwks_source=StaticJWKSSource(test_jwks),
        issuer=test_issuer,
        audience=test_audience,
    )


@pytest.mark.asyncio
async def test_valid_token_is_accepted_and_subject_extracted(verifier, issue_token):
    token = issue_token(subject="jdoe@corp.com")
    result = await verifier.verify_token(token)
    assert result is not None
    assert result.subject == "jdoe@corp.com"
    assert result.client_id == "22222222-test-copilot-client"
    assert result.scopes == ["access_as_user"]


@pytest.mark.asyncio
async def test_expired_token_is_rejected(verifier, issue_token):
    token = issue_token(expires_in=-60)
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(verifier, issue_token):
    token = issue_token(audience="some-other-app-id")
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_wrong_issuer_is_rejected(verifier, issue_token):
    token = issue_token(issuer="https://login.microsoftonline.com/some-other-tenant/v2.0")
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_missing_subject_claim_is_rejected(verifier, issue_token):
    token = issue_token(extra_claims={"preferred_username": None})
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_token_signed_by_a_different_key_is_rejected(verifier, test_issuer, test_audience, test_kid):
    """The real crypto check: a token that's structurally identical and
    even carries the right kid header, but was never signed by the key
    the JWKS actually holds, must be rejected on signature alone.
    """
    wrong_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": test_issuer,
            "aud": test_audience,
            "iat": now,
            "exp": now + 3600,
            "preferred_username": "attacker@corp.com",
        },
        wrong_private_key,
        algorithm="RS256",
        headers={"kid": test_kid},
    )
    assert await verifier.verify_token(forged) is None


@pytest.mark.asyncio
async def test_malformed_token_is_rejected(verifier):
    assert await verifier.verify_token("not-a-real-jwt") is None
