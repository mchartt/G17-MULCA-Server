"""
One name per member in a group: what Cadence and every channel write.

A linked group gathers people from the web, Discord and Slack. Two different
people can have the same name there ("Ada" on Discord and "Ada" on the web,
or two "Luca" in one channel). If both reach the model and the cards as
"Ada", Cadence cannot tell who said what or who should own a task, and the
members cannot either. So each member gets a name nobody else in the group
has:

  - a name used once stays as it is ("Ada");
  - the same name in different channels gets the channel: "Ada (Discord)",
    "Ada (Web)";
  - the same name twice in one channel gets a number: "Luca", "Luca 2"
    (by member id, so it does not change between two calls).

The same person in two channels is ONE member once they ran
`/coordinator me` (router._on_me): they have one id, so one name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

CHANNEL_LABELS = {"web": "Web", "discord": "Discord", "slack": "Slack"}


def distinct_names(members: Iterable[tuple[int, str, str]]) -> dict[int, str]:
    """
    (member id, display name, channel) -> member id -> a name no other member
    of the group has. The first entry of an id wins (callers list this
    channel's members first). Order of the result follows the input.
    """
    first: dict[int, tuple[str, str]] = {}
    for member_id, name, channel in members:
        first.setdefault(member_id, ((name or "member").strip() or "member", channel))

    by_name: dict[str, list[int]] = {}
    for member_id, (name, _) in first.items():
        by_name.setdefault(name.casefold(), []).append(member_id)

    result: dict[int, str] = {}
    for member_id, (name, channel) in first.items():
        same = by_name[name.casefold()]
        if len(same) == 1:
            result[member_id] = name
            continue
        channels = {first[other][1] for other in same}
        label = name
        if len(channels) > 1:
            label = f"{name} ({CHANNEL_LABELS.get(channel, channel.capitalize() or 'Other')})"
        twins = sorted(other for other in same if first[other][1] == channel)
        if len(twins) > 1:
            n = twins.index(member_id) + 1
            label = label if n == 1 else (f"{name} {n}" if len(channels) == 1 else f"{name} {n} ({CHANNEL_LABELS.get(channel, channel)})")
        result[member_id] = label
    return result


_SUFFIX = re.compile(r"(?: \d+)?(?: \((?:Web|Discord|Slack)\))?$")


def base_name(label: str) -> str:
    """The display name without what distinct_names added: "Ada 2 (Discord)" -> "Ada"."""
    return _SUFFIX.sub("", (label or "").strip()) or (label or "").strip()
