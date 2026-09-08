"""Resolving what a real EBS account can actually do, for onboarding: the
correction this module exists for is that an Entra identity and an EBS
account are two different things (an Entra UPN has no fixed relationship
to FND_USER.USER_NAME), and the Org IDs a mapping grants shouldn't be
free-typed — they should be exactly what EBS itself already grants that
FND_USER through their assigned responsibilities.

Two lookups, matching the actual onboarding sequence in the admin-gui:
  1. Given an EBS username, which responsibilities are assigned to them.
  2. Given one of those responsibilities, which Operating Units it grants.

For (2), EBS resolves accessible Operating Units two ways, and a
responsibility can have either configured: the simple MO: Operating Unit
profile option (exactly one org), or MO: Security Profile (MOAC — a named
security profile that can grant several orgs, via an org list, hierarchy,
or classification). When both could apply, Security Profile takes
precedence — that ordering is standard, documented EBS/MOAC behavior.
The exact table backing "which orgs does this security profile grant"
(MO_SEC_PROFILE_ORG_ASSIGNS below) is the one piece of this module worth
verifying against a real instance before trusting it in production; it
wasn't possible to check against a live EBS database while writing this.

GV$ isn't relevant here (this queries application tables, not dynamic
performance views), but every table is schema-qualified the same way as
mcp-server's DBA tools, for the same reason: no dependency on the
connecting account's CURRENT_SCHEMA or on a synonym existing.
"""

from __future__ import annotations

from typing import Protocol

from app.ebs.domains import resolve_domain
from app.schemas.ebs_lookup import AssignedOrganization, AssignedResponsibility

# Oracle's numeric code for "profile option set at the Responsibility
# level" — this exact value (10003) is what Oracle's own scripts and
# documentation commonly hardcode for this purpose. Worth cross-checking
# against FND_PROFILE_OPTION_LEVELS on a real instance rather than trusting
# blindly, same caveat as the security-profile assignment table above.
RESPONSIBILITY_LEVEL_ID = 10003

_RESPONSIBILITIES_SQL = """
    SELECT DISTINCT fr.responsibility_id, fr.application_id, frt.responsibility_name
    FROM APPLSYS.FND_USER fu
    JOIN APPLSYS.FND_USER_RESP_GROUPS urg
      ON urg.user_id = fu.user_id
    JOIN APPLSYS.FND_RESPONSIBILITY fr
      ON fr.responsibility_id = urg.responsibility_id
     AND fr.application_id = urg.responsibility_application_id
    JOIN APPLSYS.FND_RESPONSIBILITY_TL frt
      ON frt.responsibility_id = fr.responsibility_id
     AND frt.application_id = fr.application_id
     AND frt.language = 'US'
    WHERE fu.user_name = :username
      AND (fu.end_date IS NULL OR fu.end_date > SYSDATE)
      AND (urg.end_date IS NULL OR urg.end_date > SYSDATE)
    ORDER BY frt.responsibility_name
"""

_SECURITY_PROFILE_SQL = """
    SELECT fpov.profile_option_value
    FROM APPLSYS.FND_PROFILE_OPTION_VALUES fpov
    JOIN APPLSYS.FND_PROFILE_OPTIONS fpo
      ON fpo.profile_option_id = fpov.profile_option_id
    WHERE fpo.profile_option_name = 'MO_SECURITY_PROFILE_ID'
      AND fpov.level_id = :level_id
      AND fpov.level_value = :responsibility_id
"""

_ORGS_BY_SECURITY_PROFILE_SQL = """
    SELECT haou.organization_id, haou.name
    FROM MO_SEC_PROFILE_ORG_ASSIGNS mspoa
    JOIN HR_ALL_ORGANIZATION_UNITS haou
      ON haou.organization_id = mspoa.organization_id
    WHERE mspoa.security_profile_id = :security_profile_id
    ORDER BY haou.name
"""

_ORGS_BY_OPERATING_UNIT_SQL = """
    SELECT haou.organization_id, haou.name
    FROM APPLSYS.FND_PROFILE_OPTION_VALUES fpov
    JOIN APPLSYS.FND_PROFILE_OPTIONS fpo
      ON fpo.profile_option_id = fpov.profile_option_id
    JOIN HR_ALL_ORGANIZATION_UNITS haou
      ON haou.organization_id = TO_NUMBER(fpov.profile_option_value)
    WHERE fpo.profile_option_name = 'MO_OPERATING_UNIT'
      AND fpov.level_id = :level_id
      AND fpov.level_value = :responsibility_id
"""


class EBSLookupConnector(Protocol):
    def list_assigned_responsibilities(self, username: str) -> list[AssignedResponsibility]: ...

    def list_assigned_organizations(
        self, application_id: int, responsibility_id: int
    ) -> list[AssignedOrganization]: ...


class OracleEBSLookupConnector:
    def __init__(self, dsn: str, user: str, password: str) -> None:
        self._dsn = dsn
        self._user = user
        self._password = password

    def _connect(self):
        import oracledb

        return oracledb.connect(user=self._user, password=self._password, dsn=self._dsn)

    def list_assigned_responsibilities(self, username: str) -> list[AssignedResponsibility]:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(_RESPONSIBILITIES_SQL, {"username": username})
            return [
                AssignedResponsibility(
                    responsibility_id=row[0],
                    application_id=row[1],
                    responsibility_name=row[2],
                    domain=resolve_domain(row[1]),
                )
                for row in cursor.fetchall()
            ]

    def list_assigned_organizations(
        self, application_id: int, responsibility_id: int
    ) -> list[AssignedOrganization]:
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                _SECURITY_PROFILE_SQL,
                {"level_id": RESPONSIBILITY_LEVEL_ID, "responsibility_id": responsibility_id},
            )
            security_profile_row = cursor.fetchone()

            if security_profile_row is not None:
                cursor.execute(_ORGS_BY_SECURITY_PROFILE_SQL, {"security_profile_id": security_profile_row[0]})
                source = "security_profile"
            else:
                cursor.execute(
                    _ORGS_BY_OPERATING_UNIT_SQL,
                    {"level_id": RESPONSIBILITY_LEVEL_ID, "responsibility_id": responsibility_id},
                )
                source = "operating_unit"

            return [
                AssignedOrganization(org_id=str(row[0]), org_name=row[1], source=source)
                for row in cursor.fetchall()
            ]


class MockEBSLookupConnector:
    """Realistic canned data matching Oracle's own Vision demo dataset —
    JDOE has one responsibility resolved the simple way (MO: Operating
    Unit, one org) and one resolved via MO: Security Profile (multiple
    orgs), to exercise both paths the same way a real instance would.
    """

    def __init__(self) -> None:
        self._responsibilities: dict[str, list[AssignedResponsibility]] = {
            "JDOE": [
                AssignedResponsibility(
                    responsibility_id=20420,
                    application_id=200,
                    responsibility_name="Payables Manager",
                    domain=resolve_domain(200),
                ),
                AssignedResponsibility(
                    responsibility_id=50678,
                    application_id=101,
                    responsibility_name="General Ledger Multi-Org Reporting",
                    domain=resolve_domain(101),
                ),
            ]
        }
        self._organizations: dict[tuple[int, int], list[AssignedOrganization]] = {
            (200, 20420): [
                AssignedOrganization(org_id="204", org_name="Vision Operations", source="operating_unit"),
            ],
            (101, 50678): [
                AssignedOrganization(org_id="204", org_name="Vision Operations", source="security_profile"),
                AssignedOrganization(org_id="207", org_name="Vision Services", source="security_profile"),
                AssignedOrganization(org_id="210", org_name="Vision Italy", source="security_profile"),
            ],
        }

    def list_assigned_responsibilities(self, username: str) -> list[AssignedResponsibility]:
        return self._responsibilities.get(username.upper(), [])

    def list_assigned_organizations(
        self, application_id: int, responsibility_id: int
    ) -> list[AssignedOrganization]:
        return self._organizations.get((application_id, responsibility_id), [])
