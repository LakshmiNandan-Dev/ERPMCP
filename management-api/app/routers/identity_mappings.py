from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from app.db import identity_engine
from app.deps import admin_subject
from app.models.identity import identity_mapping_org_scope, identity_mappings
from app.schemas.identity import Environment, IdentityMappingCreate, IdentityMappingOut, TargetSystem

router = APIRouter(prefix="/identity-mappings", tags=["identity-mappings"])


def _load_with_scope(conn, mapping_id: int) -> IdentityMappingOut:
    mapping_row = conn.execute(
        select(identity_mappings).where(identity_mappings.c.id == mapping_id)
    ).mappings().first()
    if mapping_row is None:
        raise HTTPException(status_code=404, detail=f"No identity mapping with id={mapping_id}")

    scope_rows = conn.execute(
        select(identity_mapping_org_scope).where(
            identity_mapping_org_scope.c.identity_mapping_id == mapping_id
        )
    ).mappings().all()

    return IdentityMappingOut.model_validate({**mapping_row, "org_scope": scope_rows})


@router.get("", response_model=list[IdentityMappingOut])
def list_identity_mappings(
    entra_subject: str | None = None,
    environment: Environment | None = None,
    target_system: TargetSystem | None = None,
    domain: str | None = None,
    include_closed: bool = False,
    _caller: str = Depends(admin_subject),
) -> list[IdentityMappingOut]:
    query = select(identity_mappings)
    if entra_subject:
        query = query.where(identity_mappings.c.entra_subject == entra_subject)
    if environment:
        query = query.where(identity_mappings.c.environment == environment)
    if target_system:
        query = query.where(identity_mappings.c.target_system == target_system)
    if domain:
        query = query.where(identity_mappings.c.domain == domain)
    if not include_closed:
        query = query.where(identity_mappings.c.effective_end_date.is_(None))

    with identity_engine.connect() as conn:
        rows = conn.execute(query.order_by(identity_mappings.c.entra_subject)).mappings().all()
        return [_load_with_scope(conn, row["id"]) for row in rows]


@router.get("/{mapping_id}", response_model=IdentityMappingOut)
def get_identity_mapping(mapping_id: int, _caller: str = Depends(admin_subject)) -> IdentityMappingOut:
    with identity_engine.connect() as conn:
        return _load_with_scope(conn, mapping_id)


@router.post("", response_model=IdentityMappingOut, status_code=201)
def create_identity_mapping(
    body: IdentityMappingCreate, subject: str = Depends(admin_subject)
) -> IdentityMappingOut:
    # A DBA mapping has no org_scope at all (enforced by the schema
    # validator), so all(empty list) would vacuously evaluate True and
    # mislabel it "resolved_from_source" — a DBA grant is always a direct
    # admin decision, never something resolved from a source system.
    if body.target_system == "ebs_dba":
        resolution_source = "manually_overridden"
    else:
        resolution_source = (
            "resolved_from_source"
            if all(scope.resolved_from_source for scope in body.org_scope)
            else "manually_overridden"
        )

    try:
        with identity_engine.begin() as conn:
            mapping_id = conn.execute(
                insert(identity_mappings)
                .values(
                    entra_subject=body.entra_subject,
                    environment=body.environment,
                    target_system=body.target_system,
                    target_username=body.target_username,
                    domain=body.domain,
                    mapped_role=body.mapped_role,
                    resolution_source=resolution_source,
                    effective_start_date=body.effective_start_date,
                    created_by=subject,
                )
                .returning(identity_mappings.c.id)
            ).scalar_one()

            # SQLAlchemy's execute(insert(...), []) does NOT no-op on an
            # empty list — it inserts one row of DEFAULT VALUES, which then
            # fails identity_mapping_org_scope's NOT NULL columns and
            # raises an IntegrityError that the broad except below
            # mis-reports as "already has an active mapping". Caught by
            # actually running the ebs_dba path (empty org_scope by
            # design), not by inspection — guard explicitly instead of
            # relying on execute() to handle the empty case sensibly.
            if body.org_scope:
                conn.execute(
                    insert(identity_mapping_org_scope),
                    [
                        {
                            "identity_mapping_id": mapping_id,
                            "org_id": scope.org_id,
                            "org_name": scope.org_name,
                            "resolved_from_source": scope.resolved_from_source,
                        }
                        for scope in body.org_scope
                    ],
                )
    except IntegrityError as exc:
        # Almost certainly the open-ended-uniqueness constraint: this
        # subject/environment/target_system already has an active mapping.
        # Close it first via POST /{id}/close, then create the new one —
        # this is the real role-change flow, verified when the schema
        # itself was built, not something new invented at the API layer.
        raise HTTPException(
            status_code=409,
            detail=(
                "This subject already has an active mapping for this "
                "environment and target system. Close it first, then "
                "create the replacement."
            ),
        ) from exc

    with identity_engine.connect() as conn:
        return _load_with_scope(conn, mapping_id)


@router.post("/{mapping_id}/close", response_model=IdentityMappingOut)
def close_identity_mapping(
    mapping_id: int, subject: str = Depends(admin_subject)
) -> IdentityMappingOut:
    with identity_engine.begin() as conn:
        # updated_at is set explicitly here, not left to an onupdate=
        # default: that's a client-side SQLAlchemy behavior tied to the
        # exact Column object that declares it, and this module's copy of
        # the table (see models/identity.py's duplication note) doesn't
        # carry it — relying on it silently produced a null updated_at
        # here until this was caught directly against a real database.
        now = datetime.now(timezone.utc)
        result = conn.execute(
            identity_mappings.update()
            .where(identity_mappings.c.id == mapping_id)
            .where(identity_mappings.c.effective_end_date.is_(None))
            .values(effective_end_date=now, updated_at=now, updated_by=subject)
        )
        if result.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No open mapping with id={mapping_id} (already closed, or doesn't exist).",
            )

    with identity_engine.connect() as conn:
        return _load_with_scope(conn, mapping_id)
