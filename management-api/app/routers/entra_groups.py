"""Entra / AD group access grants — who may use the tools, per environment.

Same sensitivity as entra_config: this decides who is let in at all. Two
guards keep an admin from locking everyone out by accident:

  * enforcement cannot be turned on while no group is granted (see
    entra_config.put_entra_config), and
  * the last open grant cannot be closed while enforcement is on.

Grants are closed, never deleted — history stays auditable, as with identity
mappings. Changes take effect on the next tool call: mcp-server reads the
grants per request, so removing a group revokes access immediately (unlike
the tenant registration, which is deliberately only read at startup).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.db import identity_engine
from app.deps import admin_subject
from app.models.identity import entra_group_access, entra_registrations
from app.schemas.entra_groups import (
    EntraGroupInput,
    EntraGroupOut,
    EntraGroupUpdate,
    GROUP_DOMAINS,
    parse_domains,
)
from app.schemas.identity import Environment

router = APIRouter(prefix="/entra-groups", tags=["entra-groups"])


def _out(row) -> EntraGroupOut:
    data = dict(row)
    data["allowed_domains"] = parse_domains(data["allowed_domains"])
    return EntraGroupOut.model_validate(data)


def _load(conn, environment: str, group_id: int):
    return conn.execute(
        select(entra_group_access)
        .where(entra_group_access.c.id == group_id)
        .where(entra_group_access.c.environment == environment)
    ).mappings().first()


def _enforced(conn, environment: str) -> bool:
    return bool(
        conn.execute(
            select(entra_registrations.c.enforce_group_access).where(
                entra_registrations.c.environment == environment
            )
        ).scalar_one_or_none()
    )


def _open_count(conn, environment: str) -> int:
    return conn.execute(
        select(func.count())
        .select_from(entra_group_access)
        .where(entra_group_access.c.environment == environment)
        .where(entra_group_access.c.effective_end_date.is_(None))
    ).scalar_one()


@router.get("/domains")
def list_group_domains(_caller: str = Depends(admin_subject)) -> dict[str, list[str]]:
    """The domains a grant may be limited to, for the console's picker."""
    return {"domains": list(GROUP_DOMAINS)}


@router.get("/{environment}", response_model=list[EntraGroupOut])
def list_entra_groups(
    environment: Environment, include_closed: bool = False, _caller: str = Depends(admin_subject)
) -> list[EntraGroupOut]:
    stmt = select(entra_group_access).where(entra_group_access.c.environment == environment)
    if not include_closed:
        stmt = stmt.where(entra_group_access.c.effective_end_date.is_(None))
    with identity_engine.connect() as conn:
        rows = conn.execute(stmt.order_by(entra_group_access.c.group_name, entra_group_access.c.id)).mappings().all()
    return [_out(r) for r in rows]


@router.post("/{environment}", response_model=EntraGroupOut, status_code=201)
def add_entra_group(
    environment: Environment, body: EntraGroupInput, subject: str = Depends(admin_subject)
) -> EntraGroupOut:
    now = datetime.now(timezone.utc)
    try:
        with identity_engine.begin() as conn:
            new_id = conn.execute(
                insert(entra_group_access)
                .values(
                    environment=environment,
                    group_object_id=body.group_object_id,
                    group_name=body.group_name,
                    channel=body.channel,
                    allowed_domains=",".join(body.allowed_domains),
                    effective_start_date=now,
                    created_at=now,
                    created_by=subject,
                )
                .returning(entra_group_access.c.id)
            ).scalar_one()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Group {body.group_object_id} already has an open grant for channel "
                f"{body.channel!r} in {environment!r}. Edit that grant, or close it first."
            ),
        ) from exc
    with identity_engine.connect() as conn:
        return _out(_load(conn, environment, new_id))


@router.put("/{environment}/{group_id}", response_model=EntraGroupOut)
def update_entra_group(
    environment: Environment, group_id: int, body: EntraGroupUpdate, subject: str = Depends(admin_subject)
) -> EntraGroupOut:
    try:
        with identity_engine.begin() as conn:
            result = conn.execute(
                entra_group_access.update()
                .where(entra_group_access.c.id == group_id)
                .where(entra_group_access.c.environment == environment)
                .where(entra_group_access.c.effective_end_date.is_(None))
                .values(
                    group_name=body.group_name,
                    channel=body.channel,
                    allowed_domains=",".join(body.allowed_domains),
                    updated_at=datetime.now(timezone.utc),
                    updated_by=subject,
                )
            )
            if result.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail=f"No open grant with id={group_id} in {environment!r} (closed, or doesn't exist).",
                )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Another open grant already covers this group on that channel.",
        ) from exc
    with identity_engine.connect() as conn:
        return _out(_load(conn, environment, group_id))


@router.post("/{environment}/{group_id}/close", response_model=EntraGroupOut)
def close_entra_group(
    environment: Environment, group_id: int, subject: str = Depends(admin_subject)
) -> EntraGroupOut:
    with identity_engine.begin() as conn:
        if _enforced(conn, environment) and _open_count(conn, environment) <= 1:
            existing = _load(conn, environment, group_id)
            if existing is not None and existing["effective_end_date"] is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This is the last open group grant and group enforcement is on: closing "
                        "it would lock every user out. Add another group first, or turn "
                        "enforcement off."
                    ),
                )
        now = datetime.now(timezone.utc)
        result = conn.execute(
            entra_group_access.update()
            .where(entra_group_access.c.id == group_id)
            .where(entra_group_access.c.environment == environment)
            .where(entra_group_access.c.effective_end_date.is_(None))
            .values(effective_end_date=now, updated_at=now, updated_by=subject)
        )
        if result.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No open grant with id={group_id} in {environment!r} (already closed, or doesn't exist).",
            )
    with identity_engine.connect() as conn:
        return _out(_load(conn, environment, group_id))
