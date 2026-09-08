"""Onboarding lookups against EBS itself — the two-step flow the admin-gui
walks an admin through: pick a username, see what's actually assigned to
them in EBS, pick a responsibility, see exactly what Org IDs it grants.
Nothing here is typed by the admin; it's all read from app.ebs.lookup.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.deps import admin_subject
from app.ebs.domains import KNOWN_DOMAINS
from app.ebs.factory import ebs_lookup_connector
from app.schemas.ebs_lookup import AssignedOrganization, AssignedResponsibility

router = APIRouter(prefix="/ebs-lookup", tags=["ebs-lookup"])


@router.get("/domains", response_model=list[str])
def get_known_domains(_caller: str = Depends(admin_subject)) -> list[str]:
    # For the one flow that has no FND_APPLICATION to derive a domain
    # from — a Fusion mapping, manually entered — so admin-gui offers a
    # fixed choice instead of free text, keeping it in the same set an
    # EBS lookup would resolve to. The single source of truth is
    # app.ebs.domains; this just exposes it.
    return list(KNOWN_DOMAINS)


@router.get("/users/{username}/responsibilities", response_model=list[AssignedResponsibility])
def get_assigned_responsibilities(username: str, _caller: str = Depends(admin_subject)) -> list[AssignedResponsibility]:
    responsibilities = ebs_lookup_connector.list_assigned_responsibilities(username)
    if not responsibilities:
        raise HTTPException(
            status_code=404,
            detail=f"No active responsibilities found for EBS username {username!r}.",
        )
    return responsibilities


@router.get(
    "/responsibilities/{application_id}/{responsibility_id}/organizations",
    response_model=list[AssignedOrganization],
)
def get_assigned_organizations(
    application_id: int, responsibility_id: int, _caller: str = Depends(admin_subject)
) -> list[AssignedOrganization]:
    organizations = ebs_lookup_connector.list_assigned_organizations(application_id, responsibility_id)
    if not organizations:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No Operating Units resolved for responsibility_id={responsibility_id} "
                f"(application_id={application_id}) — check its MO: Security Profile / "
                "MO: Operating Unit profile option is actually set."
            ),
        )
    return organizations
