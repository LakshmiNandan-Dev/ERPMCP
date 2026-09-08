from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.identity import Environment


class EntraRegistrationInput(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    audience: str = Field(min_length=1, max_length=120)
    subject_claim: str = Field(default="preferred_username", max_length=60)
    resource_server_url: str = Field(min_length=1, max_length=500)


class EntraRegistrationOut(BaseModel):
    id: int
    environment: Environment
    tenant_id: str
    audience: str
    subject_claim: str
    resource_server_url: str
    created_at: datetime
    created_by: str
    updated_at: datetime | None
    updated_by: str | None

    model_config = {"from_attributes": True}
