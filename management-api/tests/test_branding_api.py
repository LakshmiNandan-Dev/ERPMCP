"""Per-deployment branding — company name and logo for the admin console.

Requires IDENTITY_DB_URL pointed at a real, migrated database, same as the
other API tests here.

The property worth guarding hardest is the auth asymmetry: GET is open while
PUT is gated. That is deliberate and unique to this router — the admin console
renders its header before anyone signs in, so a gated read would show the
product default on the login screen and the customer's own name only after.
It is exactly the kind of thing a later well-meaning edit "fixes" by adding a
dependency to the GET, which is why it is asserted here rather than left to a
comment.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models.identity import branding

from tests.test_identity_mappings_api import _cleanup_engine

client = TestClient(app)
ADMIN = {"X-Admin-Subject": "admin@corp.com"}


def _cleanup() -> None:
    with _cleanup_engine.begin() as conn:
        conn.execute(branding.delete())


# ── the auth asymmetry ───────────────────────────────────────────────────────

def test_get_is_readable_without_any_credentials():
    """The header renders above the sign-in form. If this ever starts
    requiring auth, branding silently stops working on the one screen it most
    needs to work on."""
    _cleanup()
    resp = client.get("/branding")
    assert resp.status_code == 200


def test_put_is_refused_without_credentials():
    resp = client.put("/branding", json={"company_name": "Acme", "logo_data_uri": None})
    assert resp.status_code in (400, 401)  # 400 stub mode, 401 once a secret is set


# ── unconfigured is a normal state, not an error ─────────────────────────────

def test_unconfigured_returns_nulls_rather_than_404():
    """entra_config 404s when unset; branding must not. The header has to be
    able to render from the response, so "nothing configured" is data."""
    _cleanup()
    body = client.get("/branding").json()
    assert body == {
        "company_name": None,
        "logo_data_uri": None,
        "updated_at": None,
        "updated_by": None,
    }


# ── single row, upserted ─────────────────────────────────────────────────────

def test_create_then_update_keeps_exactly_one_row():
    _cleanup()
    try:
        created = client.put(
            "/branding",
            headers=ADMIN,
            json={"company_name": "Acme Corp", "logo_data_uri": "data:image/png;base64,iVBORw0KGg=="},
        )
        assert created.status_code == 200
        assert created.json()["company_name"] == "Acme Corp"
        assert created.json()["updated_by"] == "admin@corp.com"

        updated = client.put(
            "/branding",
            headers={"X-Admin-Subject": "admin2@corp.com"},
            json={"company_name": "Acme Holdings", "logo_data_uri": None},
        )
        assert updated.status_code == 200
        assert updated.json()["company_name"] == "Acme Holdings"
        assert updated.json()["updated_by"] == "admin2@corp.com"
        assert updated.json()["logo_data_uri"] is None  # cleared, not retained

        with _cleanup_engine.connect() as conn:
            assert conn.execute(branding.select()).rowcount == 1

        assert client.get("/branding").json() == updated.json()
    finally:
        _cleanup()


def test_clearing_the_name_returns_to_the_product_default():
    """Blank is normalised to null so the header falls back, rather than
    rendering an empty title bar."""
    _cleanup()
    try:
        client.put("/branding", headers=ADMIN, json={"company_name": "Acme", "logo_data_uri": None})
        cleared = client.put("/branding", headers=ADMIN, json={"company_name": "   ", "logo_data_uri": None})
        assert cleared.json()["company_name"] is None
    finally:
        _cleanup()


# ── the logo must be inline, never a URL ─────────────────────────────────────

def test_an_http_url_is_refused():
    """A URL renders on a developer's machine and silently breaks in the
    air-gapped customer network this product deploys into."""
    resp = client.put(
        "/branding",
        headers=ADMIN,
        json={"company_name": "Acme", "logo_data_uri": "https://cdn.example.com/logo.png"},
    )
    assert resp.status_code == 422


def test_a_non_image_data_uri_is_refused():
    resp = client.put(
        "/branding",
        headers=ADMIN,
        json={"company_name": "Acme", "logo_data_uri": "data:text/html;base64,PGgxPmhpPC9oMT4="},
    )
    assert resp.status_code == 422


def test_a_valid_inline_image_is_accepted():
    _cleanup()
    try:
        resp = client.put(
            "/branding",
            headers=ADMIN,
            json={"company_name": "Acme", "logo_data_uri": "data:image/svg+xml;base64,PHN2Zz4="},
        )
        assert resp.status_code == 200
        assert resp.json()["logo_data_uri"].startswith("data:image/svg+xml;base64,")
    finally:
        _cleanup()
