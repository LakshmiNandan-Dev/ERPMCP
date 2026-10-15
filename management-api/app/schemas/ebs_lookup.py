from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AssignedResponsibility(BaseModel):
    responsibility_id: int
    application_id: int
    responsibility_name: str
    # The functional pillar this responsibility's Application belongs to
    # (finance, SCM, manufacturing, HCM, ...) — resolved server-side from
    # application_id via app.ebs.domains, not typed by whoever's onboarding
    # this user. Surfaced here so admin-gui can show it before the mapping
    # is created, same as it shows the responsibility name itself.
    domain: str


class AssignedOrganization(BaseModel):
    org_id: str
    org_name: str
    # Which EBS mechanism actually granted this org — surfaced to the
    # admin-gui so an admin can see whether a wide multi-org list came
    # from a security profile or (unusually) something odd, not just a
    # silent single value.
    source: Literal["security_profile", "operating_unit"]


class AssignedPerson(BaseModel):
    """The HR person behind an FND user (FND_USER.EMPLOYEE_ID ->
    PER_ALL_PEOPLE_F). This is what an hcm mapping's ebs_person_id is
    resolved from, so "my own record" and "my reports" are anchored to what
    EBS itself says rather than to anything an admin types."""

    person_id: int
    full_name: str
    employee_number: str | None = None
