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
from dataclasses import dataclass
from typing import Literal

# "ebs_dba" is a distinct persona, not a third backend system — see
# identity-service/db/models.py. A resolved ebs_dba identity always has
# allowed_org_ids=() (all-or-nothing access, no Org ID scoping); see
# tools/registry.py's resolve_scoped_call for how that's handled downstream.
TargetSystem = Literal["ebs", "fusion", "ebs_dba"]


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


class IdentityResolver(ABC):
    @abstractmethod
    def resolve(self, subject: str, target_system: TargetSystem) -> ResolvedIdentity:
        """Resolve a caller's identity into their mapped role and scope.

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

    def resolve(self, subject: str, target_system: TargetSystem) -> ResolvedIdentity:
        for mapping in self._mappings:
            if mapping.subject == subject and mapping.target_system == target_system:
                return mapping

        raise LookupError(
            f"No identity mapping for subject={subject!r}, target_system={target_system!r}. "
            "The stub resolver only knows about the fixed mappings it was built with."
        )
