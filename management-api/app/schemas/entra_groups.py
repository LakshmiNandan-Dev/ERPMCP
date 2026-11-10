"""Entra / Active Directory group grants: which groups may use the tools, over
which channel, for which domains. See identity-service/db/models.py
(entra_group_access) for the semantics; the admin console edits these."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.ebs.domains import KNOWN_DOMAINS
from app.schemas.identity import Environment

# Mirrors identity-service/db/models.py (VALID_GROUP_CHANNELS) and mcp-server's
# policy/group_gate.py — keep in sync by hand.
GroupChannel = Literal["chat_agent", "mcp_direct", "any"]

# What a group may be limited to: the functional domains an identity mapping
# can carry, plus "dba" for the DBA toolset (a DBA mapping has no domain).
# "*" means all of them, and is explicit rather than an empty list.
GROUP_DOMAINS: tuple[str, ...] = (*KNOWN_DOMAINS, "dba")
ALL_DOMAINS = "*"

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_domains(stored: str) -> list[str]:
    return [d for d in stored.split(",") if d]


class EntraGroupInput(BaseModel):
    group_object_id: str = Field(
        description="The group's Entra object ID (a GUID) — what the token's groups claim carries."
    )
    group_name: str = Field(min_length=1, max_length=200)
    channel: GroupChannel
    allowed_domains: list[str] = Field(
        min_length=1,
        description='Domains members may use, or ["*"] for all. Never empty: "no limit" must be chosen.',
    )

    @field_validator("group_object_id")
    @classmethod
    def _guid(cls, v: str) -> str:
        v = v.strip().lower()
        if not _GUID.match(v):
            raise ValueError(
                "group_object_id must be the group's Entra object ID (a GUID such as "
                "0b3f1c2e-1111-2222-3333-444455556666), not its display name."
            )
        return v

    @field_validator("group_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("group_name must not be blank.")
        return v

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        cleaned = sorted({d.strip().lower() for d in v if d and d.strip()})
        if not cleaned:
            raise ValueError("allowed_domains must name at least one domain, or \"*\".")
        if ALL_DOMAINS in cleaned:
            if cleaned != [ALL_DOMAINS]:
                raise ValueError('"*" (all domains) cannot be combined with named domains.')
            return cleaned
        unknown = [d for d in cleaned if d not in GROUP_DOMAINS]
        if unknown:
            raise ValueError(f"Unknown domain(s) {unknown}; must be from {list(GROUP_DOMAINS)} or \"*\".")
        return cleaned


class EntraGroupUpdate(BaseModel):
    """Editable fields of an open grant. The group itself (group_object_id)
    is fixed: granting a different group is a new grant, so the history of the
    old one stays intact."""

    group_name: str = Field(min_length=1, max_length=200)
    channel: GroupChannel
    allowed_domains: list[str] = Field(min_length=1)

    _name = field_validator("group_name")(EntraGroupInput._name.__func__)  # type: ignore[attr-defined]
    _domains = field_validator("allowed_domains")(EntraGroupInput._domains.__func__)  # type: ignore[attr-defined]


class EntraGroupOut(BaseModel):
    id: int
    environment: Environment
    group_object_id: str
    group_name: str
    channel: GroupChannel
    allowed_domains: list[str]
    effective_start_date: datetime
    effective_end_date: datetime | None
    created_at: datetime
    created_by: str
    updated_at: datetime | None
    updated_by: str | None
