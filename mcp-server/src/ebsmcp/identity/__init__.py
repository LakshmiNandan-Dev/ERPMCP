from .postgres_resolver import PostgresIdentityResolver
from .resolver import IdentityResolver, ResolvedIdentity, StubIdentityResolver, TargetSystem

__all__ = [
    "IdentityResolver",
    "ResolvedIdentity",
    "StubIdentityResolver",
    "PostgresIdentityResolver",
    "TargetSystem",
]
