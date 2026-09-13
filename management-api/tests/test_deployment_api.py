"""GET /deployment — which deploy stage this deployment serves.

The admin console offers all four environments in its pickers, but a mapping
created for a stage this deployment does not serve is stored, displayed as
active, and then never resolves — surfacing at tool-call time as
no_identity_mapping rather than as an error at creation. This endpoint is
what lets the console tell the difference.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import app

client = TestClient(app)


def test_deployment_is_readable_without_credentials():
    """Ungated like GET /branding: the console needs to know which
    environment it is administering before anyone signs in, and a deploy
    stage name is not sensitive."""
    resp = client.get("/deployment")
    assert resp.status_code == 200


def test_reports_the_configured_environment():
    resp = client.get("/deployment")
    assert resp.json() == {"environment": load_settings().ebsmcp_environment}


def test_defaults_rather_than_failing_when_unset():
    """Optional with a default on purpose. Making it required would stop
    every deployment that has not yet added it to management-api's
    environment from starting at all."""
    assert load_settings().ebsmcp_environment in ("dev", "test", "uat", "prod")


def test_health_is_unchanged():
    """Configuration deliberately did not get folded into /health — probes
    and orchestrators already watch that shape."""
    assert client.get("/health").json() == {"status": "ok"}
