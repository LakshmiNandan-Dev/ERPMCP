from .postgres_resolver import PostgresIdentityResolver
from .resolver import (
    ANY_DOMAIN,
    SENSITIVE_DOMAINS,
    IdentityResolver,
    ResolvedIdentity,
    StubIdentityResolver,
    TargetSystem,
    pick_by_domain,
)

__all__ = [
    "ANY_DOMAIN",
    "SENSITIVE_DOMAINS",
    "IdentityResolver",
    "ResolvedIdentity",
    "StubIdentityResolver",
    "PostgresIdentityResolver",
    "TargetSystem",
    "pick_by_domain",
]
