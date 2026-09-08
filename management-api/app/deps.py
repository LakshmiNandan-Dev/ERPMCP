"""Who's allowed to onboard or close a mapping — real local-account
bearer-token verification once ADMIN_SESSION_SECRET is set (see
app.config.Settings.has_local_admin_auth), the X-Admin-Subject header
stub until then. Same activation contract as mcp-server's own Entra
wiring: nothing else in this module's callers needs to change once real
auth is configured, because they only ever see a validated subject
string either way.

The stub is deliberately still here, not deleted now that real auth
exists: it's what every local/no-accounts-yet run (including this
project's own test suite) keeps using, exactly as EBSMCP_DEV_IDENTITY_SUBJECT
remains mcp-server's fallback.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from app.auth.local import verify_session_token
from app.config import load_settings

_settings = load_settings()


def admin_subject(
    x_admin_subject: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    if _settings.has_local_admin_auth:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="Authorization: Bearer <token> is required.",
            )
        subject = verify_session_token(authorization[len("Bearer ") :], secret=_settings.admin_session_secret)  # type: ignore[arg-type]
        if subject is None:
            raise HTTPException(status_code=401, detail="Invalid or expired admin session.")
        return subject

    if not x_admin_subject:
        raise HTTPException(
            status_code=400,
            detail="X-Admin-Subject header is required (stub for real admin auth).",
        )
    return x_admin_subject
