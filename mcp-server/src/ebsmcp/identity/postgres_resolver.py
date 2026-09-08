"""Real identity resolution, replacing StubIdentityResolver: looks up the
caller's currently-open mapping (effective_end_date IS NULL — the same
invariant enforced at the schema layer by identity-service's partial
unique index) and its Org ID scope from the identity-mapping database.

This is the on-prem, low-latency path the architecture doc calls for:
identity resolution sits in the hot path of every tool call, so it's a
direct indexed DB read here, not an HTTP call to management-api (which
stays reserved for admin onboarding, out of that hot path).
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, select

from ebsmcp.identity.resolver import IdentityResolver, ResolvedIdentity, TargetSystem
from ebsmcp.identity.tables import (
    identity_mapping_instance_scope,
    identity_mapping_org_scope,
    identity_mappings,
)


class PostgresIdentityResolver(IdentityResolver):
    def __init__(self, db_url: str, environment: str, engine: Engine | None = None) -> None:
        self._engine = engine or create_engine(db_url)
        self._environment = environment

    def resolve(self, subject: str, target_system: TargetSystem) -> ResolvedIdentity:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    identity_mappings.c.id,
                    identity_mappings.c.mapped_role,
                    identity_mappings.c.instance_scope_restricted,
                ).where(
                    identity_mappings.c.entra_subject == subject,
                    identity_mappings.c.environment == self._environment,
                    identity_mappings.c.target_system == target_system,
                    identity_mappings.c.effective_end_date.is_(None),
                )
            ).first()

            if row is None:
                raise LookupError(
                    f"No identity mapping for subject={subject!r}, "
                    f"environment={self._environment!r}, target_system={target_system!r}."
                )

            mapping_id, mapped_role, instance_scope_restricted = row
            org_ids = conn.execute(
                select(identity_mapping_org_scope.c.org_id).where(
                    identity_mapping_org_scope.c.identity_mapping_id == mapping_id
                )
            ).scalars().all()

            allowed_instances: tuple[str, ...] | None = None
            if instance_scope_restricted:
                allowed_instances = tuple(
                    conn.execute(
                        select(identity_mapping_instance_scope.c.instance_name).where(
                            identity_mapping_instance_scope.c.identity_mapping_id == mapping_id
                        )
                    ).scalars().all()
                )

        return ResolvedIdentity(
            subject=subject,
            environment=self._environment,
            target_system=target_system,
            mapped_role=mapped_role,
            allowed_org_ids=tuple(org_ids),
            allowed_instances=allowed_instances,
        )
