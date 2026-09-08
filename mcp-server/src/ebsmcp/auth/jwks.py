"""Where the token verifier gets the public key it needs to check a
token's signature. Split into a Protocol specifically so the real Entra
deployment path (fetch + cache from Entra's own JWKS endpoint) and the
test path (a fixed, self-signed key set — no network, no real tenant) are
interchangeable: EntraTokenVerifier never knows which one it's holding.
"""

from __future__ import annotations

from typing import Any, Protocol

import jwt


class JWKSSource(Protocol):
    def get_signing_key(self, kid: str) -> jwt.PyJWK:
        """Return the public key matching this token's `kid` header.

        Raises jwt.PyJWKClientError if no matching key is found — callers
        treat that as "cannot verify this token", never as "no restriction".
        """


class HttpJWKSSource:
    """Real deployment: fetches and caches from Entra's own JWKS endpoint,
    e.g. https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys.
    """

    def __init__(self, jwks_uri: str) -> None:
        self._client = jwt.PyJWKClient(jwks_uri, cache_keys=True)

    def get_signing_key(self, kid: str) -> jwt.PyJWK:
        return self._client.get_signing_key(kid)


class StaticJWKSSource:
    """A fixed key set, no network call — what every test in this project
    uses instead of a live Entra tenant. Build one from a self-signed RSA
    keypair's public JWK to test the full verification path (signature,
    issuer, audience, expiry) for real, without needing real Entra.
    """

    def __init__(self, jwks: dict[str, Any]) -> None:
        self._keyset = jwt.PyJWKSet.from_dict(jwks)

    def get_signing_key(self, kid: str) -> jwt.PyJWK:
        for key in self._keyset.keys:
            if key.key_id == kid:
                return key
        raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid!r}")
