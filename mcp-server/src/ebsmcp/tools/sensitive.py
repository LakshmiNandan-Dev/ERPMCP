"""Enforcement for tools that return personal or financial data (hcm,
finance): the one door those tools must go through.

Org scoping answers "which operating units", which is the right question for
inventory and the wrong one for people and money. Three more things have to
hold, and none of them can be left to each tool to remember:

1. Domain. An hcm tool runs only under an hcm mapping, a finance tool only
   under a finance one. A user onboarded for SCM is not thereby entitled to
   payroll. resolve_sensitive_call asks the resolver for exactly that domain
   and re-checks the answer.
2. Row scope (hcm). Whose record. The caller's own, plus their reporting
   line, unless an admin has explicitly granted "all". Expressed as a SQL
   predicate (person_predicate) built from the resolved identity — never from
   a tool argument, so a model cannot ask for someone else's record into
   scope.
3. Field scope. Which columns of a record the caller may see. Salary,
   national ID, bank account and the like are withheld unless that class was
   explicitly granted to the mapping; the caller's own record is the one
   exception (OWN_RECORD_SENSITIVE_VISIBLE). Withheld columns are emitted as
   NULL in the SELECT — never fetched and filtered afterwards — and the
   response says they were withheld, so the model does not read an absent
   value as "no salary on file".

Everything the call released or withheld is written to the audit record under
`_security`. Parameters that may themselves be personal (a name being
searched for) should go through audit_safe.

Tools in tools/hcm and tools/finance must not call resolve_scoped_call
directly; tests/test_sensitive_security.py fails the build if one does, and
resolve_scoped_call itself refuses a sensitive domain it was not entered
through here for.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Literal, Sequence

from mcp.server.mcpserver.exceptions import ToolError

from ebsmcp.connectors.base import EBSConnector
from ebsmcp.identity import ResolvedIdentity
from ebsmcp.tools.registry import ToolContext, resolve_scoped_call

# Mirrored in identity-service/db/models.py (VALID_FIELD_CLASSES) and
# management-api/app/schemas/identity.py — keep all three in sync by hand.
FieldClass = Literal[
    "compensation",
    "national_id",
    "date_of_birth",
    "bank_account",
    "tax_id",
    "medical",
    "home_contact",
]
FIELD_CLASSES: tuple[str, ...] = (
    "compensation",
    "national_id",
    "date_of_birth",
    "bank_account",
    "tax_id",
    "medical",
    "home_contact",
)

SensitiveDomain = Literal["hcm", "finance"]

# Whether a caller sees their own sensitive fields without a grant — the
# ordinary self-service case (your own pay, your own bank details). Flip to
# False to require an explicit grant even for your own record.
OWN_RECORD_SENSITIVE_VISIBLE = True

_PERSON_BIND = "sec_person_id"
# Reporting-line depth limit; a supervisor loop is cut by NOCYCLE anyway.
_MAX_REPORT_LEVELS = 15


@dataclass(frozen=True)
class FieldSpec:
    """One selectable output column. field_class=None means not sensitive."""

    expression: str
    alias: str
    field_class: FieldClass | None = None


@dataclass
class SensitiveScope:
    identity: ResolvedIdentity
    effective_org_ids: tuple[str, ...]
    connector: EBSConnector
    domain: SensitiveDomain
    _security: dict[str, Any]

    # ── row scope ────────────────────────────────────────────────────────────

    def person_predicate(self, person_column: str) -> tuple[str, dict[str, Any]]:
        """WHERE fragment restricting `person_column` (a PERSON_ID) to the
        people this caller may see. Fail-closed: no HR person on the mapping
        is denial unless the mapping was explicitly granted "all"."""
        scope = self.identity.hr_scope
        self._security["person_scope"] = scope
        if scope == "all":
            return "1 = 1", {}
        if self.identity.person_id is None:
            raise ToolError(
                "This identity mapping is not tied to an HR person, so no personal "
                "records can be shown. Ask an administrator to re-onboard the mapping."
            )
        binds: dict[str, Any] = {_PERSON_BIND: self.identity.person_id}
        if scope == "self":
            return f"{person_column} = :{_PERSON_BIND}", binds
        if scope == "reports":
            return (
                f"({person_column} = :{_PERSON_BIND} OR {person_column} IN ("
                "SELECT paaf.person_id FROM APPS.PER_ALL_ASSIGNMENTS_F paaf "
                "WHERE TRUNC(SYSDATE) BETWEEN paaf.effective_start_date "
                "AND paaf.effective_end_date "
                "AND paaf.primary_flag = 'Y' AND paaf.assignment_type = 'E' "
                f"START WITH paaf.supervisor_id = :{_PERSON_BIND} "
                "CONNECT BY NOCYCLE PRIOR paaf.person_id = paaf.supervisor_id "
                f"AND LEVEL <= {_MAX_REPORT_LEVELS}))",
                binds,
            )
        raise ToolError(f"Unknown hr_scope {scope!r} on this identity mapping.")

    # ── field scope ──────────────────────────────────────────────────────────

    def select_fields(
        self,
        fields: Sequence[FieldSpec],
        person_column: str | None = None,
    ) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
        """Render a SELECT list with sensitive columns gated for this caller.

        Returns (select_sql, binds, withheld). Granted classes are selected
        plainly. Otherwise, if the caller's own record may show them and
        person_column says whose row it is, the column is wrapped in a CASE
        so it is non-NULL only on the caller's own row; if not, it is a
        literal NULL and the real column is never read. `withheld` lists what
        was held back and from whom, for the response.
        """
        granted = self.identity.sensitive_grants
        parts: list[str] = []
        binds: dict[str, Any] = {}
        withheld: list[dict[str, str]] = []
        requested: set[str] = set()
        released: set[str] = set()

        for spec in fields:
            if spec.field_class is None or spec.field_class in granted:
                parts.append(f"{spec.expression} AS {spec.alias}")
                if spec.field_class is not None:
                    requested.add(spec.field_class)
                    released.add(spec.field_class)
                continue

            requested.add(spec.field_class)
            own_ok = (
                OWN_RECORD_SENSITIVE_VISIBLE
                and person_column is not None
                and self.identity.person_id is not None
            )
            if own_ok:
                parts.append(
                    f"CASE WHEN {person_column} = :{_PERSON_BIND} "
                    f"THEN {spec.expression} END AS {spec.alias}"
                )
                binds[_PERSON_BIND] = self.identity.person_id
                withheld.append(
                    {"field": spec.alias, "field_class": spec.field_class,
                     "withheld_from": "records other than your own"}
                )
            else:
                parts.append(f"NULL AS {spec.alias}")
                withheld.append(
                    {"field": spec.alias, "field_class": spec.field_class,
                     "withheld_from": "all records"}
                )

        self._security["sensitive_requested"] = sorted(requested)
        self._security["sensitive_released"] = sorted(released)
        self._security["sensitive_withheld"] = sorted({w["field_class"] for w in withheld})
        return ", ".join(parts), binds, withheld

    def disclosure(self, withheld: list[dict[str, str]]) -> dict[str, Any]:
        """The part of the response that tells the model (and the user)
        what it is not seeing, so an absent value is not read as zero."""
        return {
            "domain": self.domain,
            "person_scope": self.identity.hr_scope if self.domain == "hcm" else None,
            "withheld_fields": withheld,
            "granted_field_classes": sorted(self.identity.sensitive_grants),
        }


def audit_safe(params: dict[str, Any], personal_keys: Sequence[str]) -> dict[str, Any]:
    """Params for the audit record with personal values replaced by a marker
    that records only that the argument was supplied. Use for any argument
    that is itself a name, number or identifier of a person."""
    return {
        k: ("<redacted>" if k in personal_keys and v is not None else v)
        for k, v in params.items()
    }


@contextmanager
def resolve_sensitive_call(
    ctx: ToolContext,
    *,
    tool_name: str,
    domain: SensitiveDomain,
    params: dict[str, Any],
    requested_org_ids: list[str] | None = None,
    requested_instance: str | None = None,
) -> Iterator[SensitiveScope]:
    """The only way a tools/hcm or tools/finance tool resolves a call.

    Wraps resolve_scoped_call with the domain pinned, re-verifies the
    resolved mapping really is in that domain, and hands back a
    SensitiveScope carrying the row/field-scope helpers. The call is audited
    like any other, with a `_security` block describing what was released
    and withheld.
    """
    if domain not in ("hcm", "finance"):
        raise ValueError(f"resolve_sensitive_call is for hcm/finance, got {domain!r}")

    security: dict[str, Any] = {"domain": domain}
    with resolve_scoped_call(
        ctx,
        tool_name=tool_name,
        target_system="ebs",
        params=params,
        requested_org_ids=requested_org_ids,
        requested_instance=requested_instance,
        domains=(domain,),
        security=security,
    ) as (identity, effective_org_ids, connector):
        # Defence in depth: a custom resolver that ignored `domains` must not
        # be able to hand a mapping from another domain to a sensitive tool.
        if identity.domain != domain:
            raise ToolError(
                f"This tool requires a {domain!r} identity mapping; the caller's "
                f"mapping is in domain {identity.domain!r}."
            )
        security["person_id_present"] = identity.person_id is not None
        yield SensitiveScope(
            identity=identity,
            effective_org_ids=effective_org_ids,
            connector=connector,
            domain=domain,
            _security=security,
        )
