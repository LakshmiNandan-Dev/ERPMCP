"""Mountable-toolset pattern.

Straight from the tool-selection discussion in the architecture doc: sixty
tools in one flat MCP server is a real selection-accuracy risk, not a
cosmetic one. The fix isn't better tool descriptions, it's not exposing all
sixty at once. A ToolSet is one category (concurrent processing, security,
space, ...) with its own registration function; a deployment mounts only
the toolsets relevant to it, and which toolsets a given caller sees can
later be filtered by their mapped responsibility, the same entitlement
mapping already used for data access.

This core slice mounts exactly one toolset (health) to prove the pattern.
Every real DBA toolset described in the catalog plugs in the same way,
later.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ebsmcp.audit import AuditLogger
from ebsmcp.connectors.base import EBSConnector
from ebsmcp.identity import IdentityResolver, ResolvedIdentity, TargetSystem
from ebsmcp.policy import EntitlementFilter


@dataclass
class ToolContext:
    """Shared dependencies every tool registration function receives.
    Nothing here is toolset-specific — it's the same four collaborators
    from the request flow (connector, identity, entitlement, audit) that
    every tool in every category needs.
    """

    connectors: dict[str, EBSConnector]
    identity_resolver: IdentityResolver
    entitlement: EntitlementFilter
    audit: AuditLogger
    environment: str
    dev_subject: str


@dataclass
class ToolSet:
    name: str
    register: Callable[[MCPServer, ToolContext], None]


def mount_toolsets(app: MCPServer, ctx: ToolContext, toolsets: list[ToolSet]) -> None:
    for toolset in toolsets:
        toolset.register(app, ctx)


def current_subject(ctx: ToolContext) -> str:
    """Who's actually calling, right now.

    get_access_token() is the mcp SDK's own accessor for the current
    request's validated token (populated by its auth middleware once
    EntraTokenVerifier accepts a bearer token — see server.py). It's None
    on stdio, and on streamable-http before a real Entra registration
    exists, in which case dev_subject is exactly the stand-in it always
    was. Once Entra auth is live, this line is what stops being a stub —
    no other code in the request path needs to change.
    """
    token = get_access_token()
    return token.subject if token and token.subject else ctx.dev_subject


def permitted_instances(ctx: ToolContext, identity: ResolvedIdentity) -> set[str]:
    """Which configured EBS instances this identity may reach at all —
    the same intersection _resolve_instance uses to auto-select or reject
    a specific request, exposed separately for tools (list_ebs_instances)
    that need the whole set rather than one resolved connector.

    allowed_instances=None means the identity isn't instance-scoped at all
    (every mapping created before this concept existed, and any mapping
    that simply hasn't been restricted) — any configured instance is fair
    game. An empty tuple is a deliberate "zero instances" grant, not "no
    restriction recorded" — see ResolvedIdentity's docstring.
    """
    allowed = identity.allowed_instances
    return set(ctx.connectors) if allowed is None else set(ctx.connectors) & set(allowed)


def _resolve_instance(
    ctx: ToolContext, identity: ResolvedIdentity, requested_instance: str | None
) -> str:
    """Pick which configured EBS instance this call targets."""
    if requested_instance is not None:
        name = requested_instance.upper()
        if name not in ctx.connectors:
            raise ToolError(
                f"Unknown EBS instance {requested_instance!r}. "
                f"Configured instances: {', '.join(sorted(ctx.connectors)) or '(none)'}."
            )
        allowed = identity.allowed_instances
        if allowed is not None and name not in allowed:
            raise ToolError(
                f"subject={identity.subject!r} is not permitted to reach EBS instance {name!r}."
            )
        return name

    candidates = permitted_instances(ctx, identity)

    if len(candidates) == 1:
        return next(iter(candidates))

    if not candidates:
        raise ToolError(
            f"subject={identity.subject!r} has no permitted EBS instance in this deployment."
        )

    raise ToolError(
        "Multiple EBS instances are available and none was specified — pass "
        f"instance to pick one of: {', '.join(sorted(candidates))}. "
        "Call list_ebs_instances to see this list directly."
    )


@contextmanager
def resolve_identity_only(
    ctx: ToolContext,
    *,
    tool_name: str,
    target_system: TargetSystem,
    params: dict[str, Any],
) -> Iterator[ResolvedIdentity]:
    """The lightweight sibling of resolve_scoped_call, for tools that
    don't touch EBS data at all — right now, just list_ebs_instances.
    Still requires a real identity mapping and is still audited (so an
    unmapped caller can't discover what a mapped one could reach), but
    deliberately skips entitlement scoping and instance/connector
    resolution — resolve_scoped_call always resolves ONE instance, which
    would defeat a tool whose entire point is listing them before one is
    picked.
    """
    subject = current_subject(ctx)

    with ctx.audit.audit_call(
        tool_name=tool_name,
        subject=subject,
        environment=ctx.environment,
        target_system=target_system,
        params=params,
    ):
        try:
            identity = ctx.identity_resolver.resolve(subject, target_system)
        except LookupError as exc:
            raise ToolError(str(exc)) from exc

        yield identity


@contextmanager
def resolve_scoped_call(
    ctx: ToolContext,
    *,
    tool_name: str,
    target_system: TargetSystem,
    params: dict[str, Any],
    requested_org_ids: list[str] | None = None,
    requested_instance: str | None = None,
) -> Iterator[tuple[ResolvedIdentity, tuple[str, ...], EBSConnector]]:
    """The shared shape every tool follows: resolve identity, apply the
    entitlement filter, resolve which EBS instance to hit, audit the call,
    and translate anticipated failures into ToolError so the client
    actually sees is_error=True with a real reason — rather than every one
    of the DBA catalog's tools re-implementing this by hand.

    Yields (identity, effective_org_ids, connector) for the tool body to
    use. Raises ToolError (never lets a bare LookupError escape) if the
    caller has no identity mapping; EntitlementDenied — itself a
    ToolError — propagates unchanged if the requested scope has no
    permitted overlap.

    ebs_dba is all-or-nothing (see identity-service/db/models.py) — a
    resolved ebs_dba identity always has allowed_org_ids=(), so
    EntitlementFilter.scope_org_ids would reject it outright even though
    the caller is fully entitled. Org-scoping simply doesn't apply to this
    persona: the entitlement check for ebs_dba is entirely "did
    identity_resolver.resolve() succeed" (an open ebs_dba mapping exists),
    already handled above — there's no narrower scope left to apply.
    """
    subject = current_subject(ctx)

    with ctx.audit.audit_call(
        tool_name=tool_name,
        subject=subject,
        environment=ctx.environment,
        target_system=target_system,
        params=params,
    ) as outcome:
        try:
            identity = ctx.identity_resolver.resolve(subject, target_system)
        except LookupError as exc:
            raise ToolError(str(exc)) from exc

        if target_system == "ebs_dba":
            effective_org_ids: tuple[str, ...] = ()
        else:
            effective_org_ids = ctx.entitlement.scope_org_ids(identity, requested_org_ids)
        outcome["effective_org_ids"] = effective_org_ids

        instance = _resolve_instance(ctx, identity, requested_instance)
        outcome["instance"] = instance

        yield identity, effective_org_ids, ctx.connectors[instance]
