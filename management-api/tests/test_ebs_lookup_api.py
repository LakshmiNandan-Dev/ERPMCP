"""Exercises the two onboarding lookups against MockEBSLookupConnector's
canned data (no real EBS instance exists yet — see app/ebs/lookup.py).
Covers both Org ID resolution paths: the simple MO: Operating Unit case
(one org) and the MO: Security Profile case (several orgs).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

# These lookups are admin-gui-facing reads and now require an
# authenticated caller too, same as every other endpoint — see
# app/deps.py's admin_subject.
client = TestClient(app, headers={"X-Admin-Subject": "admin@corp.com"})


def test_known_user_returns_assigned_responsibilities():
    resp = client.get("/ebs-lookup/users/JDOE/responsibilities")
    assert resp.status_code == 200
    names = {r["responsibility_name"] for r in resp.json()}
    assert names == {"Payables Manager", "General Ledger Multi-Org Reporting"}


def test_unknown_user_is_404():
    resp = client.get("/ebs-lookup/users/NOBODY/responsibilities")
    assert resp.status_code == 404


def test_operating_unit_path_resolves_a_single_org():
    resp = client.get("/ebs-lookup/responsibilities/200/20420/organizations")
    assert resp.status_code == 200
    orgs = resp.json()
    assert orgs == [{"org_id": "204", "org_name": "Vision Operations", "source": "operating_unit"}]


def test_security_profile_path_resolves_multiple_orgs():
    resp = client.get("/ebs-lookup/responsibilities/101/50678/organizations")
    assert resp.status_code == 200
    orgs = resp.json()
    assert {o["org_id"] for o in orgs} == {"204", "207", "210"}
    assert all(o["source"] == "security_profile" for o in orgs)


def test_unknown_responsibility_is_404():
    resp = client.get("/ebs-lookup/responsibilities/999/99999/organizations")
    assert resp.status_code == 404
