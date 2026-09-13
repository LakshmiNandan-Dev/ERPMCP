"""Two database connections, matching the two schemas this API sits in
front of — identity-service's identity_mappings/identity_mapping_org_scope,
and audit-service's audit_log. Same IDENTITY_DB_URL/AUDIT_DB_URL env var
names those services' own Alembic setups use, so one connection string
per environment configures both the schema owner and this API consistently.

admin_subject is a stub, same pattern as mcp-server's
EBSMCP_DEV_IDENTITY_SUBJECT: real admin authentication (who's allowed to
create/close mappings) is a later integration, not solved here. Every
write endpoint requires an X-Admin-Subject header instead, recorded as
created_by/updated_by — enough to keep the audit trail honest without
building an auth system to get there.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    identity_db_url: str
    audit_db_url: str

    # EBS read-only connection for onboarding lookups (assigned
    # responsibilities and organizations) — same env var names as
    # mcp-server's EBS connector. Unset means "no real EBS instance yet",
    # same fallback-to-mock pattern used throughout this project.
    ebs_db_dsn: str | None = Field(default=None, alias="EBS_DB_DSN")
    ebs_db_user: str | None = Field(default=None, alias="EBS_DB_USER")
    ebs_db_password: str | None = Field(default=None, alias="EBS_DB_PASSWORD")

    # Real admin authentication for this API itself — who's allowed to
    # onboard/close identity mappings — as opposed to entra_registrations
    # (a separate table/concern: the tenant config mcp-server's own OAuth
    # resource server validates M365 Copilot bearer tokens against).
    #
    # Deliberately local accounts (app.models.identity.admin_accounts),
    # not Entra/SSO: admin access to this tool is meant to stay independent
    # of whichever customer Entra tenant a deployment happens to serve —
    # this is the product's own control plane, not something that should
    # go dark if a customer's tenant does, and a licensable multi-tenant
    # product can't assume every deployment even has one.
    #
    # Same fallback pattern as everywhere else in this project: unset
    # means "no local accounts set up yet" (see
    # management-api/scripts/create_admin.py to create the first one),
    # every write endpoint keeps accepting the X-Admin-Subject header stub
    # via app.deps.admin_subject; setting it switches to real
    # username/password sign-in and session-token verification
    # automatically, no other code changes.
    admin_session_secret: str | None = Field(default=None, alias="ADMIN_SESSION_SECRET")

    # Which deploy stage this deployment serves — the same value mcp-server
    # reads, and the one identity mappings are matched against at resolution
    # time. This API had no notion of it, so the admin console offered all
    # four environments unconditionally: a mapping created for a stage this
    # deployment does not serve is stored and shown as active, then fails at
    # tool-call time as no_identity_mapping rather than at creation.
    #
    # Optional with a default rather than required, deliberately: making it
    # mandatory would stop every existing deployment that has not yet added
    # it to management-api's environment from starting at all, turning a
    # diagnostic improvement into an outage. "dev" matches .env.example.
    ebsmcp_environment: str = Field(default="dev", alias="EBSMCP_ENVIRONMENT")

    @property
    def has_real_ebs_connection(self) -> bool:
        return bool(self.ebs_db_dsn and self.ebs_db_user and self.ebs_db_password)

    @property
    def has_local_admin_auth(self) -> bool:
        return bool(self.admin_session_secret)


@lru_cache
def load_settings() -> Settings:
    # Cached so every module's own `_settings = load_settings()` (deps,
    # db, ebs/factory, routers/auth) ends up holding the exact same
    # object, not four independent snapshots of the environment — that's
    # what lets a test flip admin_session_secret on one reference and
    # have every other module's admin_subject/login check see it too.
    return Settings()
