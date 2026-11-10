"""Validation of Entra group grants — pure schema tests, no database."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.entra_groups import (
    ALL_DOMAINS,
    GROUP_DOMAINS,
    EntraGroupInput,
    EntraGroupUpdate,
    parse_domains,
)

GUID = "0B3F1C2E-1111-2222-3333-444455556666"


def make(**over):
    base = dict(group_object_id=GUID, group_name="EBS Finance", channel="chat_agent", allowed_domains=["*"])
    return EntraGroupInput(**{**base, **over})


def test_object_id_is_trimmed_and_lower_cased():
    assert make(group_object_id=f"  {GUID}  ").group_object_id == GUID.lower()


@pytest.mark.parametrize("bad", ["EBS Finance Users", "", "0b3f1c2e", "0b3f1c2e-1111-2222-3333-44445555666",
                                 "0b3f1c2e-1111-2222-3333-44445555666g", f"{GUID} extra"])
def test_a_display_name_or_malformed_id_is_refused(bad):
    with pytest.raises(ValidationError, match="object ID"):
        make(group_object_id=bad)


def test_name_is_trimmed_and_cannot_be_blank():
    assert make(group_name="  Fin  ").group_name == "Fin"
    with pytest.raises(ValidationError):
        make(group_name="   ")


@pytest.mark.parametrize("channel", ["chat_agent", "mcp_direct", "any"])
def test_valid_channels(channel):
    assert make(channel=channel).channel == channel


def test_unknown_channel_is_refused():
    with pytest.raises(ValidationError):
        make(channel="email")


def test_domains_are_normalised_deduplicated_and_sorted():
    assert make(allowed_domains=["Finance", "dba", "finance"]).allowed_domains == ["dba", "finance"]


def test_star_means_all_and_cannot_mix_with_named_domains():
    assert make(allowed_domains=["*"]).allowed_domains == [ALL_DOMAINS]
    with pytest.raises(ValidationError, match="cannot be combined"):
        make(allowed_domains=["*", "finance"])


def test_empty_domains_are_refused_so_no_limit_is_always_a_visible_choice():
    for empty in ([], [""], ["  "]):
        with pytest.raises(ValidationError):
            make(allowed_domains=empty)


def test_unknown_domain_is_refused_and_dba_is_known():
    with pytest.raises(ValidationError, match="Unknown domain"):
        make(allowed_domains=["payroll"])
    assert "dba" in GROUP_DOMAINS and "finance" in GROUP_DOMAINS and "hcm" in GROUP_DOMAINS


def test_update_cannot_change_the_group_and_applies_the_same_rules():
    u = EntraGroupUpdate(group_name=" X ", channel="any", allowed_domains=["SCM"])
    assert u.group_name == "X" and u.allowed_domains == ["scm"]
    assert "group_object_id" not in EntraGroupUpdate.model_fields
    with pytest.raises(ValidationError):
        EntraGroupUpdate(group_name="X", channel="any", allowed_domains=["nope"])


def test_parse_domains():
    assert parse_domains("dba,finance") == ["dba", "finance"]
    assert parse_domains("*") == ["*"]
    assert parse_domains("") == []
