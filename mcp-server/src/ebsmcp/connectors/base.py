"""EBS connector interface, plus the two standing SQL conventions agreed on
for every tool that queries EBS: GV$ instead of V$ (portable across
single-instance and RAC without conditional logic), and every real
product-schema object (APPLSYS., AD., etc.) fully schema-qualified (no
dependency on the calling account's CURRENT_SCHEMA — onboarding a new
client's read-only account reduces to "grant SELECT on this exact list of
fully-qualified objects").

Revision (2026-09-02): SYS.-qualification of catalog/dynamic-performance
objects (SYS.DBA_*, SYS.GV$*) was dropped after live testing on a real EBS
instance showed the SYS.-qualified form fails even for an account that
already holds adequate underlying privilege (SELECT_CATALOG_ROLE or
equivalent), while the bare form — resolved through Oracle's standard
public synonyms, present on every stock Oracle install via catalog.sql —
succeeds. The privilege requirement itself is unchanged (a read-only
account still needs SELECT_CATALOG_ROLE for these), only how the object is
*referenced* changed. Bare GV$*/DBA_* names are therefore exempt from the
qualification requirement below; a real product-schema table like
FND_CONCURRENT_REQUESTS still must be schema-qualified, since that's a
different object with no comparable public-synonym guarantee.

validate_sql_conventions() enforces both conventions at call time rather
than leaving them as something to remember — every concrete connector
runs every query through it before execution.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

# Oracle's own dummy table needs no schema prefix in practice — SYS.DUAL
# works but nobody writes it that way, and requiring it here would be
# pedantry rather than a real portability or provisioning concern.
_QUALIFICATION_EXEMPT = {"DUAL"}

# GV$*/DBA_* resolve via Oracle's standard public synonyms on any stock
# install — see the module docstring's 2026-09-02 revision note. A prefix
# check, not an exact-name allowlist: covers every catalog/dynamic-
# performance view without having to enumerate them.
_BARE_NAME_EXEMPT_PREFIXES = ("GV$", "DBA_")

_BARE_V_DOLLAR = re.compile(r"(?<![A-Za-z])V\$", re.IGNORECASE)
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$#]*"
_FROM_JOIN_TARGET = re.compile(
    rf"\b(?:FROM|JOIN)\s+({_IDENTIFIER}(?:\.{_IDENTIFIER})?)\b(?!\s*\()",
    re.IGNORECASE,
)


class UnqualifiedSQLError(ValueError):
    """Raised when a tool's SQL violates the GV$ / schema-qualification
    convention. This fails the call outright rather than warning, because
    the whole point of the convention is that it's never optional.
    """


def validate_sql_conventions(sql: str) -> None:
    violations: list[str] = []

    bare_v = _BARE_V_DOLLAR.findall(sql)
    if bare_v:
        violations.append(
            f"found {len(bare_v)} bare V$ reference(s) — use GV$ instead, "
            "so the same query works on single-instance and RAC."
        )

    for match in _FROM_JOIN_TARGET.finditer(sql):
        target = match.group(1)
        target_upper = target.upper()
        if target_upper in _QUALIFICATION_EXEMPT:
            continue
        if target_upper.startswith(_BARE_NAME_EXEMPT_PREFIXES):
            continue
        if "." not in target:
            violations.append(
                f"unqualified object {target!r} — every FROM/JOIN target must be "
                "SCHEMA.OBJECT, e.g. APPLSYS.FND_CONCURRENT_REQUESTS (GV$*/DBA_* "
                "catalog views are exempt — see module docstring)."
            )

    if violations:
        raise UnqualifiedSQLError(
            "SQL violates the GV$/schema-qualification convention:\n  - "
            + "\n  - ".join(violations)
        )


class EBSConnector(ABC):
    @abstractmethod
    def execute_query(self, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Subclasses implement the actual query execution here. Callers
        should never call this directly — call run() instead, which
        validates the SQL convention first regardless of which concrete
        connector (real Oracle, or the in-memory mock) is in use.
        """

    def run(self, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        validate_sql_conventions(sql)
        return self.execute_query(sql, binds)
