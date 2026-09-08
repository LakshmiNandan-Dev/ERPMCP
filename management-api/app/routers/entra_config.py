"""Entra ID tenant/audience registration — one per environment.

Deliberately treated as more sensitive than identity-mapping CRUD: this
defines which identity provider and which tokens mcp-server trusts at
all, not just who maps to what scope within that trust. A change here
does not take effect until mcp-server restarts and re-reads it at
startup — never hot-reloaded, so a save can't silently redirect trust
while the server is running unattended.

The admin_subject stub applies the same way it does everywhere else in
this API — real admin authorization (ideally a distinct, more privileged
check than ordinary mapping management) is still a pending integration,
noted here because this is exactly the kind of write that most needs it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import insert, select

from app.db import identity_engine
from app.deps import admin_subject
from app.models.identity import entra_registrations
from app.schemas.entra_config import EntraRegistrationInput, EntraRegistrationOut
from app.schemas.identity import Environment

router = APIRouter(prefix="/entra-config", tags=["entra-config"])


@router.get("/{environment}", response_model=EntraRegistrationOut)
def get_entra_config(environment: Environment, _caller: str = Depends(admin_subject)) -> EntraRegistrationOut:
    with identity_engine.connect() as conn:
        row = conn.execute(
            select(entra_registrations).where(entra_registrations.c.environment == environment)
        ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Entra registration configured for environment={environment!r} yet.",
        )
    return EntraRegistrationOut.model_validate(row)


@router.put("/{environment}", response_model=EntraRegistrationOut)
def put_entra_config(
    environment: Environment, body: EntraRegistrationInput, subject: str = Depends(admin_subject)
) -> EntraRegistrationOut:
    with identity_engine.begin() as conn:
        existing_id = conn.execute(
            select(entra_registrations.c.id).where(entra_registrations.c.environment == environment)
        ).scalar_one_or_none()

        if existing_id is None:
            conn.execute(
                insert(entra_registrations).values(
                    environment=environment,
                    tenant_id=body.tenant_id,
                    audience=body.audience,
                    subject_claim=body.subject_claim,
                    resource_server_url=body.resource_server_url,
                    created_by=subject,
                )
            )
        else:
            # updated_at set explicitly — see identity_mappings.py's
            # close_identity_mapping for why onupdate= isn't relied on here.
            conn.execute(
                entra_registrations.update()
                .where(entra_registrations.c.id == existing_id)
                .values(
                    tenant_id=body.tenant_id,
                    audience=body.audience,
                    subject_claim=body.subject_claim,
                    resource_server_url=body.resource_server_url,
                    updated_at=datetime.now(timezone.utc),
                    updated_by=subject,
                )
            )

    with identity_engine.connect() as conn:
        row = conn.execute(
            select(entra_registrations).where(entra_registrations.c.environment == environment)
        ).mappings().first()
    return EntraRegistrationOut.model_validate(row)
