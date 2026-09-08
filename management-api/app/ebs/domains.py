"""EBS Application -> functional pillar mapping.

`domain` groups a functional identity mapping by EBS pillar (finance,
SCM, manufacturing, HCM, ...) so tool/data access can eventually be
scoped by pillar as well as by Org ID — see identity-service/db/models.py's
comment on the `domain` column. Derived from FND_APPLICATION.APPLICATION_ID
because EBS already uses that to group its own modules; an admin picking a
responsibility during onboarding never types a domain by hand, the same
way they never type an Org ID by hand once a responsibility is selected.

APPLICATION_ID is seed data (part of Oracle's AOL/FND application
registration, not customer-configurable), so these keys are stable across
EBS installations. Only application IDs actually seen in an onboarded
responsibility need an entry here — this doesn't need to enumerate the
full E-Business Suite catalog up front.
"""

from __future__ import annotations

# APPLICATION_ID -> domain slug.
EBS_APPLICATION_DOMAINS: dict[int, str] = {
    101: "finance",  # SQLGL — General Ledger
    200: "finance",  # SQLAP — Payables
    222: "finance",  # AR — Receivables
    660: "scm",  # PO — Purchasing
    401: "scm",  # INV — Inventory
    700: "manufacturing",  # BOM — Bills of Material
    703: "manufacturing",  # WIP — Work in Process
    800: "hcm",  # PER — Human Resources
    801: "hcm",  # PAY — Payroll
}

# The canonical, admin-gui-facing list. "other" is the catch-all for a
# Fusion mapping (no FND_APPLICATION to derive a domain from — manual
# selection instead) or an EBS application not yet in the table above, so
# onboarding is never blocked on this list being incomplete.
KNOWN_DOMAINS: tuple[str, ...] = ("finance", "scm", "manufacturing", "hcm", "other")


def resolve_domain(application_id: int) -> str:
    return EBS_APPLICATION_DOMAINS.get(application_id, "other")
