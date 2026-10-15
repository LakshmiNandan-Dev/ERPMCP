"""Identity resolution: Entra/AD subject -> EBS/Fusion mapping.

This is the "Resolve identity" step from the architecture doc's request
flow. The real implementation reads the identity-mapping store (Postgres or
Oracle, per environment) built up during onboarding — an Entra user mapped
to an EBS responsibility, with an Org ID list resolved from that
responsibility's own MO: Security Profile / MO: Operating Unit setup, never
hand-typed.

That store doesn't exist yet in this core slice. IdentityResolver is the
interface every caller depends on; StubIdentityResolver is an in-memory
stand-in with one fixed mapping, so the rest of the pipeline (entitlement
filtering, connectors, audit logging) can be built and tested against a
real shape today, and swapped for the Postgres/Oracle-backed implementation
later without touching any calling code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Sequence, TypeVar

# "ebs_dba" is a distinct persona, not a third backend system — see
# identity-service/db/models.py. A resolved ebs_dba identity always has
# allowed_org_ids=() (all-or-nothing access, no Org ID scoping); see
# tools/registry.py's resolve_scoped_call for how that's handled downstream.
TargetSystem = Literal["ebs", "fusion", "ebs_dba"]


# Domains whose data is personal or financial. A mapping in one of these is
# never picked for a call that did not ask for it by name — see
# pick_by_domain — so an hcm or finance mapping's Org IDs and grants cannot
# leak into an unrelated tool.
SENSITIVE_DOMAINS = frozenset({"hcm", "finance"})

# Passed as `domains` by the few tools that expose no business data at all
# (the pipeline smoke test) and so may run under any mapping. Never use it
# for a tool that returns rows.
ANY_DOMAIN: tuple[str, ...] = ("*",)

_T = TypeVar("_T")


def pick_by_domain(candidates: Sequence[_T], domains: tuple[str, ...] | None) -> _T | None:
    """Choose which of a subject's open mappings serves a call.

    Deterministic, never arbitrary. `domains` is a preference order: the
    first domain with an open mapping wins (see the unlabelled-candidate
    fallback below). domains=None means the tool
    declared nothing, so only non-sensitive-domain mappings are eligible
    (lowest id first, i.e. candidates are expected pre-sorted); ANY_DOMAIN
    admits everything. Candidates need a `.domain` attribute (NULL for
    ebs_dba). Returns None when nothing qualifies — callers must treat that
    as denial, never as "no restriction".
    """
    if domains is None:
        eligible = [c for c in candidates if getattr(c, "domain", None) not in SENSITIVE_DOMAINS]
        return eligible[0] if eligible else None
    if domains == ANY_DOMAIN:
        return candidates[0] if candidates else None
    for domain in domains:
        for candidate in candidates:
            if getattr(candidate, "domain", None) == domain:
                return candidate
    # A functional mapping always has a domain (the schema requires it), so a
    # candidate without one is the dev stub or a hand-built identity. It may
    # serve a non-sensitive family as a last resort, but never hcm/finance:
    # an unlabelled mapping must not be able to read personal or financial
    # data just because nothing says it cannot.
    if not any(d in SENSITIVE_DOMAINS for d in domains):
        for candidate in candidates:
            if getattr(candidate, "domain", None) is None:
                return candidate
    return None


@dataclass(frozen=True)
class ResolvedIdentity:
    """What a caller is allowed to do, resolved from their Entra identity.

    allowed_org_ids is deliberately a tuple, not a list — it should never be
    mutated after resolution. The entitlement filter treats this as the
    ceiling a request's own parameters can narrow but never widen.

    allowed_instances follows a deliberately different convention from
    allowed_org_ids: None means "not instance-scoped" (every mapping ever
    resolved before this concept existed, and any mapping nobody has
    explicitly restricted) — any configured EBS instance is reachable. An
    empty tuple is a real, explicit "zero instances" grant (deny all), and
    a non-empty tuple is an allowlist, intersected the same way Org IDs
    are. Reusing allowed_org_ids's "empty means deny" convention here would
    have meant every existing identity mapping silently loses all EBS
    access the moment this field exists, since none of them ever recorded
    an instance restriction — see tools/registry.py's _resolve_instance.
    """

    subject: str
    environment: str
    target_system: TargetSystem
    mapped_role: str
    allowed_org_ids: tuple[str, ...]
    allowed_instances: tuple[str, ...] | None = None
    # The functional pillar of the mapping that was picked (None for ebs_dba).
    domain: str | None = None
    # The caller's own HR person. None means "not tied to a person", which
    # for hcm data is denial, not "see everyone".
    person_id: int | None = None
    # How far from person_id this identity may see: self, reports, all.
    # Defaults to the narrowest so a hand-built identity never widens.
    hr_scope: str = "self"
    # Sensitive field classes explicitly granted (see tools/sensitive.py).
    # Empty means none: the caller sees them only on their own record.
    sensitive_grants: frozenset[str] = field(default_factory=frozenset)


class IdentityResolver(ABC):
    @abstractmethod
    def resolve(
        self,
        subject: str,
        target_system: TargetSystem,
        domains: tuple[str, ...] | None = None,
    ) -> ResolvedIdentity:
        """Resolve a caller's identity into their mapped role and scope.

        domains is the calling tool's domain preference order — see
        pick_by_domain for how it selects among a subject's open mappings.

        Raises LookupError if the subject has no mapping for target_system —
        callers must treat that as "deny", never as "no restriction".
        """


class StubIdentityResolver(IdentityResolver):
    """In-memory stand-in for local development, pending the real
    Postgres/Oracle-backed identity-mapping store described in the
    architecture doc's onboarding flow.

    Holds any number of fixed mappings, matched on (subject, target_system)
    — not subject alone. A real person can hold both a functional and a
    DBA persona mapping at once (two rows, two target_system values), and
    matching on subject alone would silently hand back the wrong mapping's
    target_system instead of raising LookupError for a persona a caller
    doesn't actually have.
    """

    def __init__(self, *mappings: ResolvedIdentity) -> None:
        self._mappings = mappings

    def resolve(
        self,
        subject: str,
        target_system: TargetSystem,
        domains: tuple[str, ...] | None = None,
    ) -> ResolvedIdentity:
        candidates = [
            m for m in self._mappings
            if m.subject == subject and m.target_system == target_system
        ]
        picked = pick_by_domain(candidates, domains)
        if picked is not None:
            return picked

        raise LookupError(
            f"No identity mapping for subject={subject!r}, target_system={target_system!r}. "
            "The stub resolver only knows about the fixed mappings it was built with."
        )
