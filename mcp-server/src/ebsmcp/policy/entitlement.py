"""Entitlement filtering: "Apply entitlement filter" in the request flow.

The rule this module exists to enforce, stated once so every tool doesn't
have to reinvent it: a tool call's own parameters may narrow the resolved
identity's scope, but must never widen it. An LLM-constructed tool call is
not a trusted source of authorization — only ResolvedIdentity.allowed_org_ids
is.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from ebsmcp.identity import ResolvedIdentity


class EntitlementDenied(ToolError):
    """Raised when a request's requested scope has no overlap with what the
    caller is actually allowed to see.

    Subclasses ToolError deliberately: this is an anticipated failure, not a
    crash, so a well-behaved MCP client sees is_error=True with this exact
    message — the caller should see *why* nothing came back — rather than
    the generic "Error executing tool X" a client gets for an unanticipated
    exception. Tools must let this propagate; never catch it and return a
    normal-looking result, which would hide the failure from any client that
    doesn't know to inspect the payload for an app-specific status field.
    """


class EntitlementFilter:
    def scope_org_ids(
        self,
        identity: ResolvedIdentity,
        requested_org_ids: list[str] | None = None,
    ) -> tuple[str, ...]:
        """Resolve the Org IDs a query is actually allowed to run against.

        requested_org_ids=None means "the caller didn't ask to narrow it" —
        the result is the identity's full allowed set. A non-empty list is
        intersected against that set; anything the caller asked for but
        isn't entitled to is silently dropped, never silently granted.

        Raises EntitlementDenied if the intersection is empty — either the
        identity has no allowed Org IDs at all, or every requested Org ID
        was outside the allowed set.
        """
        allowed = set(identity.allowed_org_ids)

        if requested_org_ids is None:
            effective = allowed
        else:
            effective = allowed.intersection(requested_org_ids)

        if not effective:
            raise EntitlementDenied(
                f"subject={identity.subject!r} has no permitted Org ID overlap "
                f"for this request (allowed={sorted(allowed)}, "
                f"requested={requested_org_ids})."
            )

        # Sorted for deterministic bind-variable ordering — makes audit log
        # entries and query plans reproducible across identical calls.
        return tuple(sorted(effective))
