"""Group-based access gate: may this caller use the tools at all, over this
channel, for this domain — decided from Entra / Active Directory group
membership.

Off by default. It is on only for an environment whose entra_registrations row
has enforce_group_access set, and then it fails closed: a caller whose groups
cannot be determined is denied, not waved through.

Where the groups come from
  * chat agent   — the host asserts them through ebsmcp.context
                   (set_caller_groups), together with channel "chat_agent";
  * MCP client   — the validated Entra token's groups claim (the claim name is
                   configurable, default "groups"), channel "mcp_direct".
A token that was issued with a groups *overage* (more groups than fit in a
token; Entra then sends a pointer instead of the list) cannot be checked from
the token, and is denied with that reason rather than guessed at.

What a grant says: members of group G may use the tools via channel C, limited
to domains D (or "*" for all). Domains are the identity-mapping domains plus
"dba" for the DBA toolset. A grant only ever narrows what the caller's own
identity mapping already allows; it never adds access to anything.

Grants are read on every call, so closing one revokes access immediately.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import Engine, create_engine, select

from ebsmcp.identity import ANY_DOMAIN
from ebsmcp.identity.tables import entra_group_access, entra_registrations
from ebsmcp.policy.entitlement import EntitlementDenied

# Mirrors identity-service/db/models.py (VALID_GROUP_CHANNELS) and
# management-api's schemas/entra_groups.py — keep in sync by hand.
CHANNELS = ("chat_agent", "mcp_direct", "any")
ALL_DOMAINS = "*"
DBA_DOMAIN = "dba"
DEFAULT_GROUPS_CLAIM = "groups"


class GroupAccessDenied(EntitlementDenied):
    """Subclasses EntitlementDenied so the audit record's status is "denied" —
    a denial by policy, not a fault — and a client sees the real reason."""


@dataclass(frozen=True)
class Grant:
    group_object_id: str
    channel: str
    allowed_domains: frozenset[str]


@dataclass(frozen=True)
class GroupPolicy:
    enforced: bool
    groups_claim: str
    grants: tuple[Grant, ...]


@dataclass(frozen=True)
class CallerGroups:
    """`groups` is None when membership could not be determined at all;
    `note` explains a None, or an empty set that looks like a misconfiguration."""

    groups: frozenset[str] | None
    note: str | None = None


class GroupPolicySource(ABC):
    @abstractmethod
    def load(self, environment: str) -> GroupPolicy:
        """The environment's current policy. Never cached by the caller."""


class StaticGroupPolicySource(GroupPolicySource):
    """Fixed policy, for tests and local runs."""

    def __init__(self, policy: GroupPolicy) -> None:
        self._policy = policy

    def load(self, environment: str) -> GroupPolicy:
        return self._policy


class PostgresGroupPolicySource(GroupPolicySource):
    def __init__(self, db_url: str, engine: Engine | None = None) -> None:
        self._engine = engine or create_engine(db_url, pool_pre_ping=True)

    def load(self, environment: str) -> GroupPolicy:
        with self._engine.connect() as conn:
            reg = conn.execute(
                select(entra_registrations.c.enforce_group_access, entra_registrations.c.groups_claim)
                .where(entra_registrations.c.environment == environment)
            ).first()
            if reg is None or not reg.enforce_group_access:
                return GroupPolicy(False, reg.groups_claim if reg else DEFAULT_GROUPS_CLAIM, ())
            rows = conn.execute(
                select(
                    entra_group_access.c.group_object_id,
                    entra_group_access.c.channel,
                    entra_group_access.c.allowed_domains,
                )
                .where(entra_group_access.c.environment == environment)
                .where(entra_group_access.c.effective_end_date.is_(None))
            ).all()
        grants = tuple(
            Grant(
                r.group_object_id.strip().lower(),
                r.channel,
                frozenset(d for d in r.allowed_domains.split(",") if d),
            )
            for r in rows
        )
        return GroupPolicy(True, reg.groups_claim or DEFAULT_GROUPS_CLAIM, grants)


def groups_from_claims(claims: Mapping[str, Any] | None, claim_name: str) -> CallerGroups:
    """The caller's group object IDs from a validated token's claims."""
    if claims is None:
        return CallerGroups(None, "the request carries no validated token")
    value = claims.get(claim_name)
    if isinstance(value, (list, tuple)):
        return CallerGroups(frozenset(str(g).strip().lower() for g in value if g))
    # Entra's overage marker: too many groups for the token, so it carries a
    # pointer (claim_names -> groups) instead of the list. Not checkable here.
    names = claims.get("_claim_names")
    if isinstance(names, dict) and claim_name in names:
        return CallerGroups(
            None,
            f"the token has too many groups to list them (groups overage); "
            f"'{claim_name}' is not in the token",
        )
    return CallerGroups(
        frozenset(),
        f"the token has no '{claim_name}' claim — check the app registration emits group claims",
    )


class GroupGate:
    def __init__(self, source: GroupPolicySource, environment: str) -> None:
        self._source = source
        self._environment = environment

    def authorize(
        self,
        *,
        channel: str,
        claims: Mapping[str, Any] | None,
        injected_groups: frozenset[str] | None,
        target_system: str,
        domains: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        """Raise GroupAccessDenied, or return the `domains` the identity
        resolver should be given — narrowed to what the caller's groups allow.
        With enforcement off, `domains` comes back unchanged."""
        policy = self._source.load(self._environment)
        if not policy.enforced:
            return domains

        caller = (
            CallerGroups(injected_groups)
            if injected_groups is not None
            else groups_from_claims(claims, policy.groups_claim)
        )
        if caller.groups is None:
            raise GroupAccessDenied(
                f"Access denied: your Entra group membership could not be determined "
                f"({caller.note}). Group access is enforced for this environment."
            )

        matching = [
            g for g in policy.grants
            if g.group_object_id in caller.groups and g.channel in (channel, "any")
        ]
        if not matching:
            hint = f" ({caller.note})" if caller.note and not caller.groups else ""
            raise GroupAccessDenied(
                f"Access denied: you are not a member of any Entra group authorised to use "
                f"these tools through the {channel} channel{hint}."
            )

        allowed: set[str] = set()
        for g in matching:
            allowed |= g.allowed_domains
        everything = ALL_DOMAINS in allowed

        if domains == ANY_DOMAIN:
            return domains  # a tool that exposes no business data: membership is enough
        if target_system == "ebs_dba":
            if not everything and DBA_DOMAIN not in allowed:
                raise GroupAccessDenied(
                    "Access denied: your Entra groups do not authorise the DBA tools."
                )
            return domains
        if domains is None:
            return domains  # no domain declared: not a domain-specific tool
        narrowed = tuple(d for d in domains if everything or d in allowed)
        if not narrowed:
            raise GroupAccessDenied(
                f"Access denied: your Entra groups do not authorise the {list(domains)} domain(s) "
                "this tool belongs to."
            )
        return narrowed
