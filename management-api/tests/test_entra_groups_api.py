"""Entra group grants over HTTP, against the real identity database (same
setup as test_entra_config_api.py): CRUD, the two lock-out guards, and that a
routine tenant edit can never switch enforcement off by omission."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models.identity import entra_group_access, entra_registrations

from tests.test_identity_mappings_api import _cleanup_engine

ADMIN = {"X-Admin-Subject": "admin@corp.com"}
client = TestClient(app, headers=ADMIN)

ENV = "test"
G1 = "0b3f1c2e-1111-2222-3333-444455556666"
G2 = "9a8b7c6d-aaaa-bbbb-cccc-ddddeeeeffff"
TENANT = {"tenant_id": "t", "audience": "a", "resource_server_url": "https://x/"}


def _clean() -> None:
    with _cleanup_engine.begin() as conn:
        conn.execute(entra_group_access.delete().where(entra_group_access.c.environment == ENV))
        conn.execute(entra_registrations.delete().where(entra_registrations.c.environment == ENV))


def _grant(group=G1, channel="chat_agent", domains=("*",), name="EBS Finance"):
    return client.post(
        f"/entra-groups/{ENV}",
        json={"group_object_id": group, "group_name": name, "channel": channel,
              "allowed_domains": list(domains)},
    )


def setup_function() -> None:
    _clean()


def teardown_function() -> None:
    _clean()


def test_requires_admin_subject():
    resp = TestClient(app).get(f"/entra-groups/{ENV}")
    assert resp.status_code == 400


def test_add_then_list_records_who_and_when():
    created = _grant(domains=("finance", "dba"))
    assert created.status_code == 201
    body = created.json()
    assert body["created_by"] == "admin@corp.com"
    assert body["allowed_domains"] == ["dba", "finance"]
    assert body["effective_end_date"] is None
    assert [g["id"] for g in client.get(f"/entra-groups/{ENV}").json()] == [body["id"]]


def test_a_display_name_instead_of_an_object_id_is_a_422():
    assert _grant(group="EBS Finance Users").status_code == 422


def test_the_same_group_and_channel_cannot_be_granted_twice_but_another_channel_can():
    assert _grant().status_code == 201
    dup = _grant()
    assert dup.status_code == 409 and "already has an open grant" in dup.json()["detail"]
    assert _grant(channel="mcp_direct").status_code == 201


def test_update_changes_the_editable_fields_only():
    gid = _grant().json()["id"]
    resp = client.put(
        f"/entra-groups/{ENV}/{gid}",
        json={"group_name": "Renamed", "channel": "any", "allowed_domains": ["scm"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["group_name"], body["channel"], body["allowed_domains"]) == ("Renamed", "any", ["scm"])
    assert body["group_object_id"] == G1 and body["updated_by"] == "admin@corp.com"


def test_update_into_a_collision_is_a_409():
    _grant(channel="chat_agent")
    other = _grant(channel="mcp_direct").json()["id"]
    resp = client.put(
        f"/entra-groups/{ENV}/{other}",
        json={"group_name": "x", "channel": "chat_agent", "allowed_domains": ["*"]},
    )
    assert resp.status_code == 409


def test_close_hides_the_grant_but_keeps_it_for_history():
    gid = _grant().json()["id"]
    closed = client.post(f"/entra-groups/{ENV}/{gid}/close")
    assert closed.status_code == 200 and closed.json()["effective_end_date"] is not None
    assert client.get(f"/entra-groups/{ENV}").json() == []
    history = client.get(f"/entra-groups/{ENV}?include_closed=true").json()
    assert [g["id"] for g in history] == [gid]
    assert client.post(f"/entra-groups/{ENV}/{gid}/close").status_code == 404


def test_a_closed_group_can_be_granted_again():
    gid = _grant().json()["id"]
    client.post(f"/entra-groups/{ENV}/{gid}/close")
    assert _grant().status_code == 201


def test_grants_are_per_environment():
    _grant()
    assert client.get("/entra-groups/uat").json() == []


def test_domains_endpoint_lists_dba():
    domains = client.get("/entra-groups/domains").json()["domains"]
    assert "dba" in domains and "finance" in domains


# ── the lock-out guards ──────────────────────────────────────────────────────


def test_enforcement_cannot_be_turned_on_with_no_group_granted():
    resp = client.put(f"/entra-config/{ENV}", json={**TENANT, "enforce_group_access": True})
    assert resp.status_code == 409 and "no Entra group is granted" in resp.json()["detail"]
    assert client.get(f"/entra-config/{ENV}").status_code == 404   # nothing was saved


def test_enforcement_can_be_turned_on_once_a_group_exists_and_off_again():
    _grant()
    on = client.put(f"/entra-config/{ENV}", json={**TENANT, "enforce_group_access": True, "groups_claim": "grp"})
    assert on.status_code == 200
    assert on.json()["enforce_group_access"] is True and on.json()["groups_claim"] == "grp"
    off = client.put(f"/entra-config/{ENV}", json={**TENANT, "enforce_group_access": False})
    assert off.json()["enforce_group_access"] is False


def test_the_last_open_group_cannot_be_closed_while_enforcement_is_on():
    gid = _grant().json()["id"]
    client.put(f"/entra-config/{ENV}", json={**TENANT, "enforce_group_access": True})
    resp = client.post(f"/entra-groups/{ENV}/{gid}/close")
    assert resp.status_code == 409 and "lock every user out" in resp.json()["detail"]
    # with a second group, the first can go
    _grant(group=G2, name="Other")
    assert client.post(f"/entra-groups/{ENV}/{gid}/close").status_code == 200


def test_a_routine_tenant_edit_does_not_switch_enforcement_off():
    _grant()
    client.put(f"/entra-config/{ENV}", json={**TENANT, "enforce_group_access": True, "groups_claim": "grp"})
    after = client.put(f"/entra-config/{ENV}", json={**TENANT, "tenant_id": "t2"})   # fields omitted
    assert after.status_code == 200
    assert after.json()["tenant_id"] == "t2"
    assert after.json()["enforce_group_access"] is True and after.json()["groups_claim"] == "grp"


def test_first_save_defaults_to_not_enforced_and_the_groups_claim():
    body = client.put(f"/entra-config/{ENV}", json=TENANT).json()
    assert body["enforce_group_access"] is False and body["groups_claim"] == "groups"
