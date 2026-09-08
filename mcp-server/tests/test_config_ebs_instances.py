"""Settings.resolved_ebs_instances and server.build_connectors: the config
layer for reaching more than one EBS database from a single deployment.

Uses monkeypatch.setenv (auto-cleaned per test) rather than mutating
os.environ directly — Settings() is a fresh pydantic-settings read on every
construction, so each test gets an isolated environment.
"""

from __future__ import annotations

from ebsmcp.config import Settings
from ebsmcp.connectors import MockEBSConnector, OracleEBSConnector
from ebsmcp.server import build_connectors, build_transport_security


def test_no_ebs_config_at_all_resolves_to_no_instances(monkeypatch):
    monkeypatch.delenv("EBSMCP_EBS_INSTANCES", raising=False)
    monkeypatch.delenv("EBS_DB_DSN", raising=False)

    assert Settings().resolved_ebs_instances == {}


def test_legacy_singular_vars_synthesize_one_instance_named_for_the_environment(monkeypatch):
    monkeypatch.delenv("EBSMCP_EBS_INSTANCES", raising=False)
    monkeypatch.setenv("EBS_DB_DSN", "legacy-dsn")
    monkeypatch.setenv("EBS_DB_USER", "ebsmcp_ro")
    monkeypatch.setenv("EBS_DB_PASSWORD", "secret")
    monkeypatch.setenv("EBSMCP_ENVIRONMENT", "dev")

    instances = Settings().resolved_ebs_instances

    assert set(instances) == {"DEV"}
    assert instances["DEV"].dsn == "legacy-dsn"
    assert instances["DEV"].user == "ebsmcp_ro"
    assert instances["DEV"].password == "secret"


def test_ebs_instances_json_wins_over_legacy_singular_vars(monkeypatch):
    monkeypatch.setenv("EBS_DB_DSN", "legacy-dsn")
    monkeypatch.setenv("EBS_DB_USER", "legacy-user")
    monkeypatch.setenv("EBS_DB_PASSWORD", "legacy-pw")
    monkeypatch.setenv(
        "EBSMCP_EBS_INSTANCES",
        '{"prod": {"dsn": "prod-dsn", "user": "ebsmcp_ro", "password": "p1"}, '
        '"uat": {"dsn": "uat-dsn", "user": "ebsmcp_ro", "password": "p2"}}',
    )

    instances = Settings().resolved_ebs_instances

    # Keys are normalized to upper case regardless of how they were typed
    # in the env var — instance names are matched case-insensitively
    # downstream (see tools/registry.py's _resolve_instance).
    assert set(instances) == {"PROD", "UAT"}
    assert instances["PROD"].dsn == "prod-dsn"
    assert "legacy-dsn" not in {cfg.dsn for cfg in instances.values()}


def test_build_connectors_falls_back_to_mock_with_no_real_ebs_config(monkeypatch):
    monkeypatch.delenv("EBSMCP_EBS_INSTANCES", raising=False)
    monkeypatch.delenv("EBS_DB_DSN", raising=False)
    monkeypatch.setenv("EBSMCP_ENVIRONMENT", "dev")

    connectors = build_connectors(Settings())

    assert set(connectors) == {"DEV"}
    assert isinstance(connectors["DEV"], MockEBSConnector)


def test_build_connectors_builds_one_oracle_connector_per_configured_instance(monkeypatch):
    monkeypatch.setenv(
        "EBSMCP_EBS_INSTANCES",
        '{"PROD": {"dsn": "prod-dsn", "user": "ebsmcp_ro", "password": "p1"}, '
        '"UAT": {"dsn": "uat-dsn", "user": "ebsmcp_ro", "password": "p2"}}',
    )

    connectors = build_connectors(Settings())

    assert set(connectors) == {"PROD", "UAT"}
    assert all(isinstance(c, OracleEBSConnector) for c in connectors.values())


def test_transport_security_defaults_to_empty_fail_closed_lists(monkeypatch):
    monkeypatch.delenv("EBSMCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("EBSMCP_ALLOWED_ORIGINS", raising=False)

    security = build_transport_security(Settings())

    assert security.allowed_hosts == []
    assert security.allowed_origins == []
    # Empty lists, not a permissive default — DNS-rebinding protection
    # stays on and rejects every host/origin until configured.
    assert security.enable_dns_rebinding_protection is True


def test_transport_security_reads_configured_hosts_and_origins(monkeypatch):
    monkeypatch.setenv("EBSMCP_ALLOWED_HOSTS", '["mcp.client.com", "localhost:8080"]')
    monkeypatch.setenv("EBSMCP_ALLOWED_ORIGINS", '["https://mcp.client.com"]')

    security = build_transport_security(Settings())

    assert security.allowed_hosts == ["mcp.client.com", "localhost:8080"]
    assert security.allowed_origins == ["https://mcp.client.com"]
