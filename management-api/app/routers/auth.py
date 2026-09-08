"""Local admin sign-in — see app.config.Settings.admin_session_secret for
why this is local accounts, not Entra/SSO.

Only meaningful once ADMIN_SESSION_SECRET is set and at least one row
exists in admin_accounts (see scripts/create_admin.py); until then this
endpoint always fails closed with 503 rather than pretending to be a
usable login flow — admin-gui falls back to the X-Admin-Subject stub in
that state instead of showing a login form that can never succeed.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.auth.local import create_session_token, verify_password
from app.config import load_settings
from app.db import identity_engine
from app.models.identity import admin_accounts
from app.schemas.auth import LoginRequest, LoginResponse

router = APIRouter(prefix="/auth", tags=["auth"])

_settings = load_settings()


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    if not _settings.has_local_admin_auth:
        raise HTTPException(status_code=503, detail="Local admin sign-in is not configured on this deployment.")

    with identity_engine.connect() as conn:
        row = conn.execute(
            select(admin_accounts.c.username, admin_accounts.c.password_hash, admin_accounts.c.is_active).where(
                admin_accounts.c.username == body.username
            )
        ).first()

    # Same generic failure for "no such user" and "wrong password" —
    # distinguishing them in the response would tell an attacker which
    # usernames exist.
    invalid = HTTPException(status_code=401, detail="Invalid username or password.")
    if row is None or not row.is_active:
        raise invalid
    if not verify_password(body.password, row.password_hash):
        raise invalid

    token = create_session_token(username=row.username, secret=_settings.admin_session_secret)  # type: ignore[arg-type]
    return LoginResponse(access_token=token, subject=row.username)
