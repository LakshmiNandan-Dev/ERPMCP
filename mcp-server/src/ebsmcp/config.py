"""Environment-scoped configuration.

Every setting here is read once, at process start, from environment
variables — one deployment (one Kubernetes namespace, one set of secrets)
serves exactly one entry in {dev, test, uat, prod}, per the architecture
doc's "Multi-environment approach": separate deployments, separate service
accounts, separate audit stores, never a single process serving more than
one *deploy stage*.

That's a different question from "which EBS database(s) can this
deployment reach" — a single dev-stage deployment may still need to reach
several EBS instances (PROD, UAT, QA, ...; see ebs_instances below).
Conflating the two would mean a client with multiple EBS instances needs a
separate MCP deployment per instance, which is exactly what this doesn't
require.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "uat", "prod"]
Transport = Literal["stdio", "streamable-http"]


class EBSInstanceConfig(BaseModel):
    """One target EBS database this deployment can reach."""

    dsn: str
    user: str
    password: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    environment: Environment = Field(default="dev", alias="EBSMCP_ENVIRONMENT")
    transport: Transport = Field(default="stdio", alias="EBSMCP_TRANSPORT")
    http_host: str = Field(default="0.0.0.0", alias="EBSMCP_HTTP_HOST")
    http_port: int = Field(default=8080, alias="EBSMCP_HTTP_PORT")

    # Legacy single-instance form — still supported so an existing
    # single-EBS deployment (today's demo config included) needs no
    # changes. Superseded by ebs_instances when that's set.
    ebs_db_dsn: str | None = Field(default=None, alias="EBS_DB_DSN")
    ebs_db_user: str | None = Field(default=None, alias="EBS_DB_USER")
    ebs_db_password: str | None = Field(default=None, alias="EBS_DB_PASSWORD")

    # Multi-instance form: named EBS databases this one deployment can
    # route calls to, e.g. {"PROD": {...}, "UAT": {...}, "QA": {...}}.
    # JSON-decoded from the env var by pydantic-settings.
    ebs_instances: dict[str, EBSInstanceConfig] = Field(
        default_factory=dict, alias="EBSMCP_EBS_INSTANCES"
    )

    # Local-dev convenience only: pretends the caller is this identity when
    # no real OAuth/Entra token is available. Never read once a real Entra
    # registration is configured below — the streamable-http transport
    # resolves identity from the validated bearer token instead.
    dev_identity_subject: str = Field(
        default="dev-user@example.com", alias="EBSMCP_DEV_IDENTITY_SUBJECT"
    )

    # Entra ID — unset by default (no tenant exists yet). Once a real app
    # registration exists, set entra_tenant_id and entra_audience (the API
    # app's client ID that issued tokens must carry as `aud`) and the
    # server switches from dev_identity_subject to real bearer-token
    # verification automatically; see server.py's has_real_entra_config.
    entra_tenant_id: str | None = Field(default=None, alias="EBSMCP_ENTRA_TENANT_ID")
    entra_audience: str | None = Field(default=None, alias="EBSMCP_ENTRA_AUDIENCE")
    entra_subject_claim: str = Field(default="preferred_username", alias="EBSMCP_ENTRA_SUBJECT_CLAIM")
    resource_server_url: str | None = Field(default=None, alias="EBSMCP_RESOURCE_SERVER_URL")

    # The identity-mapping database — same schema, same env var convention
    # as identity-service/management-api's IDENTITY_DB_URL. Unset means
    # "no real mapping store yet", same fallback-to-stub pattern as the EBS
    # connector.
    identity_db_url: str | None = Field(default=None, alias="IDENTITY_DB_URL")

    # python-oracledb thick mode. Thin mode (the default) cannot log in to an
    # EBS account the server authenticates with the 10g verifier — which is
    # every account when SEC_CASE_SENSITIVE_LOGON=FALSE, failing with DPY-3015.
    # Set true (and ship the Instant Client in the image) to connect to such
    # instances. Default false keeps thin mode for the mock and the tests.
    oracle_thick_mode: bool = Field(default=False, alias="EBSMCP_ORACLE_THICK_MODE")

    # streamable-http's DNS-rebinding protection: the mcp SDK's own
    # TransportSecuritySettings defaults both of these to an EMPTY list,
    # which rejects every request until a host is explicitly allowed — a
    # deliberately fail-closed default, kept here too rather than guessing
    # a permissive one. A real deployment sets both to wherever it's
    # actually reachable, e.g.
    # EBSMCP_ALLOWED_HOSTS=["mcp.client.com"]
    # EBSMCP_ALLOWED_ORIGINS=["https://mcp.client.com"]
    # (JSON arrays, same convention as ebs_instances above). Only meaningful
    # for streamable-http — stdio has no notion of a Host header.
    allowed_hosts: list[str] = Field(default_factory=list, alias="EBSMCP_ALLOWED_HOSTS")
    allowed_origins: list[str] = Field(default_factory=list, alias="EBSMCP_ALLOWED_ORIGINS")

    @property
    def has_real_ebs_connection(self) -> bool:
        return bool(self.ebs_db_dsn and self.ebs_db_user and self.ebs_db_password)

    @property
    def resolved_ebs_instances(self) -> dict[str, EBSInstanceConfig]:
        """The named EBS databases this deployment can reach.

        ebs_instances (EBSMCP_EBS_INSTANCES) wins when set. Otherwise, falls
        back to synthesizing one instance from the legacy singular
        EBS_DB_DSN/USER/PASSWORD, keyed by this deployment's own environment
        name — so an existing single-instance deployment (today's demo
        config included) needs no changes at all. Empty dict means no real
        EBS connection is configured anywhere (build_connectors falls back
        to a mock).
        """
        if self.ebs_instances:
            return {name.upper(): cfg for name, cfg in self.ebs_instances.items()}

        if self.has_real_ebs_connection:
            return {
                self.environment.upper(): EBSInstanceConfig(
                    dsn=self.ebs_db_dsn,  # type: ignore[arg-type]
                    user=self.ebs_db_user,  # type: ignore[arg-type]
                    password=self.ebs_db_password,  # type: ignore[arg-type]
                )
            }

        return {}

    @property
    def has_real_entra_config(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_audience and self.resource_server_url)

    @property
    def entra_issuer_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"

    @property
    def entra_jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def has_real_identity_db(self) -> bool:
        return bool(self.identity_db_url)


def load_settings() -> Settings:
    return Settings()
