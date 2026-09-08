"""Local admin accounts: password hashing and self-issued session tokens.

Deliberately not Entra/SSO — see app.config.Settings's admin_session_secret
comment for why admin access to this tool stays independent of whichever
customer Entra tenant a deployment happens to serve.

Session tokens are self-issued JWTs (HS256, signed with
ADMIN_SESSION_SECRET), not verified against anyone else's JWKS — this
process is both the issuer and the only verifier, so a shared symmetric
secret is the right tool, not the RSA/JWKS machinery mcp-server's
EntraTokenVerifier needs for tokens issued by a third party (Entra).
"""

from __future__ import annotations

import time

import bcrypt
import jwt

SESSION_TOKEN_TTL_SECONDS = 12 * 60 * 60  # a work day; short enough that a
# stolen token doesn't stay useful indefinitely, long enough not to force
# re-login mid-session for routine admin work.


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database — never treat that as "no
        # restriction", same as every other verification failure here.
        return False


def create_session_token(*, username: str, secret: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + SESSION_TOKEN_TTL_SECONDS},
        secret,
        algorithm="HS256",
    )


def verify_session_token(token: str, *, secret: str) -> str | None:
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    return claims.get("sub")
