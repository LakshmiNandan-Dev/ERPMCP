"""What the host application asserts about the caller, for the in-process
chat agent.

An MCP client reaching the standalone server is described by its validated
Entra bearer token. The chat agent is different: it embeds this library, has
already authenticated the user, and calls the tools directly, so there is no
token. It tells the tools who is calling through these contextvars, set per
request:

    from ebsmcp.context import set_caller_channel, set_caller_groups
    set_caller_channel("chat_agent")
    set_caller_groups(user_group_object_ids)   # None if they could not be determined

`None` for groups means "unknown", which is not the same as "no groups": with
group enforcement on, unknown is denied just as an empty set is, but the
message tells the administrator which of the two it was.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Iterable

_caller_groups: ContextVar[frozenset[str] | None] = ContextVar("ebs_caller_groups", default=None)
_caller_channel: ContextVar[str | None] = ContextVar("ebs_caller_channel", default=None)


def set_caller_groups(groups: Iterable[str] | None) -> None:
    _caller_groups.set(None if groups is None else frozenset(g.strip().lower() for g in groups if g))


def get_injected_groups() -> frozenset[str] | None:
    return _caller_groups.get()


def set_caller_channel(channel: str | None) -> None:
    _caller_channel.set(channel)


def get_injected_channel() -> str | None:
    return _caller_channel.get()
