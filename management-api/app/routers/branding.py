"""Per-deployment branding for the admin console — company name and logo.

The GET is deliberately NOT admin-gated, unlike every other route in this
API. The admin console renders its header before anyone signs in (see
admin-gui's AdminIdentityProvider, which renders its children unconditionally
and leaves subject null until sign-in), so a gated read would show the product
default on the login screen and the customer's own name only afterwards —
which is the one screen branding most needs to be right on. Nothing here is
sensitive: it is a name and a logo the customer chose to display.

Writes stay gated on admin_subject like everything else.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import insert, select

from app.db import identity_engine
from app.deps import admin_subject
from app.models.identity import branding
from app.schemas.branding import BrandingInput, BrandingOut

router = APIRouter(prefix="/branding", tags=["branding"])

_SINGLETON_ID = 1

_UNCONFIGURED = BrandingOut(
    site_name=None, company_name=None, logo_data_uri=None, updated_at=None, updated_by=None
)


@router.get("", response_model=BrandingOut)
def get_branding() -> BrandingOut:
    with identity_engine.connect() as conn:
        row = conn.execute(
            select(branding).where(branding.c.id == _SINGLETON_ID)
        ).mappings().first()
    # No row means never configured, which is a normal state, not an error:
    # the header renders the product default from these nulls.
    return _UNCONFIGURED if row is None else BrandingOut.model_validate(row)


@router.put("", response_model=BrandingOut)
def put_branding(body: BrandingInput, subject: str = Depends(admin_subject)) -> BrandingOut:
    with identity_engine.begin() as conn:
        exists = conn.execute(
            select(branding.c.id).where(branding.c.id == _SINGLETON_ID)
        ).scalar_one_or_none()

        values = {
            "site_name": body.site_name,
            "company_name": body.company_name,
            "logo_data_uri": body.logo_data_uri,
            # Set explicitly rather than relying on onupdate=, same as
            # entra_config and identity_mappings — see their comments.
            "updated_at": datetime.now(timezone.utc),
            "updated_by": subject,
        }
        if exists is None:
            conn.execute(insert(branding).values(id=_SINGLETON_ID, **values))
        else:
            conn.execute(
                branding.update().where(branding.c.id == _SINGLETON_ID).values(**values)
            )

    with identity_engine.connect() as conn:
        row = conn.execute(
            select(branding).where(branding.c.id == _SINGLETON_ID)
        ).mappings().first()
    return BrandingOut.model_validate(row)
