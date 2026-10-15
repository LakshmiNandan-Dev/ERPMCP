from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.ebs.domains import KNOWN_DOMAINS

Environment = Literal["dev", "test", "uat", "prod"]
# Mirrors identity-service/db/models.py (VALID_HR_SCOPES, VALID_FIELD_CLASSES)
# and mcp-server's tools/sensitive.py — keep in sync by hand.
HrScope = Literal["self", "reports", "all"]
FieldClass = Literal[
    "compensation",
    "national_id",
    "date_of_birth",
    "bank_account",
    "tax_id",
    "medical",
    "home_contact",
]
# Domains whose mappings carry personal or financial data. Field-class grants
# are only meaningful there; person scope only for hcm.
SENSITIVE_GRANT_DOMAINS = ("hcm", "finance")
# ebs_dba is a persona, not a third backend — see identity-service/db/
# models.py's comment on VALID_TARGET_SYSTEMS for why it doesn't fit the
# functional (Org-scoped, FND_USER-backed) shape ebs/fusion share. DBA
# access is all-or-nothing — a mapping either exists or it doesn't, no
# per-category grant — so org_scope doesn't apply to it at all.
TargetSystem = Literal["ebs", "fusion", "ebs_dba"]


class OrgScopeIn(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)
    org_name: str | None = None
    # True when this came from GET /ebs-lookup/.../organizations (the
    # admin-gui's onboarding flow selects from that list, never free
    # text) and the admin kept it checked; false if it was narrowed out
    # and re-added by hand, or entered without going through the lookup
    # at all (e.g. a Fusion mapping, which doesn't have this lookup yet).
    resolved_from_source: bool = False


class OrgScopeOut(BaseModel):
    id: int
    org_id: str
    org_name: str | None
    resolved_from_source: bool

    model_config = {"from_attributes": True}


class IdentityMappingCreate(BaseModel):
    entra_subject: str = Field(min_length=1, max_length=320)
    environment: Environment
    target_system: TargetSystem
    # Required for ebs/fusion (the actual EBS FND_USER.USER_NAME or Fusion
    # username this Entra identity maps to — a different identifier than
    # entra_subject, not a duplicate of it); must be omitted for ebs_dba,
    # whose access isn't derived from an FND_USER account at all. Enforced
    # below via model_validator, mirroring identity-service's
    # ck_identity_mappings_username_required_unless_dba constraint — this
    # check exists so the error is a clean 422 rather than a raw
    # constraint-violation surfacing from the database.
    target_username: str | None = Field(default=None, max_length=100)
    # The functional pillar (finance, scm, manufacturing, hcm, ...) this
    # mapping belongs to — required for ebs/fusion, must be omitted for
    # ebs_dba (DBA access isn't domain-scoped, same reasoning as
    # target_username). For ebs, this should be exactly the selected
    # responsibility's AssignedResponsibility.domain, not hand-typed — the
    # admin-gui relays it the same way it already relays mapped_role and
    # org_scope from the lookup response. It's also part of the
    # uniqueness key in identity-service (see that schema's comment), so
    # one person can hold an open mapping per domain at once, not just one
    # per target_system.
    domain: str | None = Field(default=None, max_length=40)
    # For ebs/fusion: the responsibility/role name, resolved from
    # target_username's real EBS/Fusion assignments — see app/ebs/lookup.py.
    # For ebs_dba: a descriptive label only (e.g. "Senior DBA — patching
    # access"); it doesn't drive access — a DBA mapping grants the full
    # DBA toolset, nothing narrower.
    mapped_role: str = Field(min_length=1, max_length=240)
    effective_start_date: datetime
    # Required (at least one entry) for ebs/fusion; must be empty for
    # ebs_dba — see _check_target_system_specific_rules.
    org_scope: list[OrgScopeIn] = Field(default_factory=list)
    # hcm only. The caller's own HR person, from GET
    # /ebs-lookup/users/{username}/person — never hand-typed. Without it an
    # hcm mapping cannot say whose records are "mine" and is denied at call
    # time, so creation requires it.
    ebs_person_id: int | None = Field(default=None, ge=1)
    # hcm only. How far from ebs_person_id the caller may see: their own
    # record, plus their reporting line (the default), or every record — the
    # last being a deliberate grant for HR specialists.
    hr_scope: HrScope = "reports"
    # hcm and finance only. Sensitive field classes this mapping may see on
    # records that are not its own. Empty (the default) withholds all of them.
    sensitive_grants: list[FieldClass] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_target_system_specific_rules(self) -> "IdentityMappingCreate":
        if self.target_system == "ebs_dba":
            if self.org_scope:
                raise ValueError(
                    "ebs_dba mappings grant the full DBA toolset — org_scope doesn't apply "
                    "and must be empty."
                )
            if self.domain is not None:
                raise ValueError("ebs_dba mappings aren't domain-scoped — domain must be omitted.")
        else:
            if not self.target_username:
                raise ValueError(
                    f"target_username is required for target_system={self.target_system!r} "
                    "(only ebs_dba mappings may omit it)."
                )
            if not self.org_scope:
                raise ValueError(
                    f"org_scope must include at least one entry for target_system={self.target_system!r}."
                )
            if not self.domain:
                raise ValueError(
                    f"domain is required for target_system={self.target_system!r} "
                    "(only ebs_dba mappings may omit it)."
                )
            if self.domain not in KNOWN_DOMAINS:
                raise ValueError(f"domain must be one of {KNOWN_DOMAINS}, got {self.domain!r}.")

        # Personal/financial-data controls. These are rejected outright where
        # they do not apply rather than silently ignored, so a mapping never
        # looks more (or less) restricted than it is.
        if self.target_system == "ebs_dba" or self.domain != "hcm":
            if self.ebs_person_id is not None:
                raise ValueError("ebs_person_id applies to hcm mappings only.")
            if self.hr_scope != "reports":
                raise ValueError("hr_scope applies to hcm mappings only.")
        elif self.ebs_person_id is None and self.hr_scope != "all":
            raise ValueError(
                "ebs_person_id is required for hcm mappings (look it up with "
                "GET /ebs-lookup/users/{username}/person); without it the mapping "
                "cannot identify which records are the caller's own."
            )
        if self.sensitive_grants:
            if self.target_system == "ebs_dba" or self.domain not in SENSITIVE_GRANT_DOMAINS:
                raise ValueError(
                    f"sensitive_grants apply to {SENSITIVE_GRANT_DOMAINS} mappings only."
                )
            self.sensitive_grants = sorted(set(self.sensitive_grants))
        return self


class IdentityMappingOut(BaseModel):
    id: int
    entra_subject: str
    environment: str
    target_system: str
    target_username: str | None
    domain: str | None
    mapped_role: str
    resolution_source: str
    effective_start_date: datetime
    effective_end_date: datetime | None
    created_at: datetime
    created_by: str
    updated_at: datetime | None
    updated_by: str | None
    org_scope: list[OrgScopeOut]
    ebs_person_id: int | None = None
    hr_scope: str = "reports"
    sensitive_grants: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}
