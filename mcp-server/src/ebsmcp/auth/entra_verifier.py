"""Real Entra ID bearer-token verification: signature (against JWKS),
issuer, audience, and expiry, all checked — implementing the mcp SDK's own
TokenVerifier protocol (mcp.server.auth.provider.TokenVerifier), which
MCPServer(token_verifier=...) accepts directly. No custom auth plumbing;
this plugs into the SDK's own OAuth resource-server support.

The subject placed on the returned AccessToken is what
ebsmcp.identity.PostgresIdentityResolver looks up in identity_mappings —
this is the one seam where "a request came in with a valid token" becomes
"here's who that is, for entitlement purposes."
"""

from __future__ import annotations

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from ebsmcp.auth.jwks import JWKSSource


class EntraTokenVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        jwks_source: JWKSSource,
        issuer: str,
        audience: str,
        subject_claim: str = "preferred_username",
    ) -> None:
        self._jwks_source = jwks_source
        self._issuer = issuer
        self._audience = audience
        self._subject_claim = subject_claim

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                return None
            signing_key = self._jwks_source.get_signing_key(kid)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            # Any failure here — bad signature, wrong issuer/audience,
            # expired, malformed — is "cannot verify", never "trust it
            # anyway". None tells the SDK's auth middleware to reject the
            # request outright.
            return None
        except jwt.PyJWKClientError:
            return None

        subject = claims.get(self._subject_claim)
        if not subject:
            return None

        scopes_claim = claims.get("scp", "")
        scopes = scopes_claim.split() if isinstance(scopes_claim, str) else []

        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("appid") or "unknown"),
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=subject,
            claims=claims,
        )
