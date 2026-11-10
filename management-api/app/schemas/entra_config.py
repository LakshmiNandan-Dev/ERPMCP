from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.identity import Environment


class EntraRegistrationInput(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    audience: str = Field(min_length=1, max_length=120)
    subject_claim: str = Field(default="preferred_username", max_length=60)
    resource_server_url: str = Field(min_length=1, max_length=500)
    # None means "leave as it is" on an update (and "off" / "groups" on a
    # first save). Deliberately not defaulted to False: a routine edit of the
    # tenant fields from a client that predates this setting must never
    # switch group enforcement off by omission.
    enforce_group_access: bool | None = None
    groups_claim: str | None = Field(default=None, min_length=1, max_length=60)


class EntraRegistrationOut(BaseModel):
    id: int
    environment: Environment
    tenant_id: str
    audience: str
    subject_claim: str
    resource_server_url: str
    enforce_group_access: bool
    groups_claim: str
    created_at: datetime
    created_by: str
    updated_at: datetime | None
    updated_by: str | None

    model_config = {"from_attributes": True}
