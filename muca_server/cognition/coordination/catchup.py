"""
Catch up (CS12): what a member missed in the other channels of their group.

A member who comes back (e.g. opens the web space on Monday after the group
talked on Discord on Thursday) asks "what did I miss?". The answer is built
from what is on disk, with no model call, so it never invents anything:

  - decisions:  the shared decision log since then (store.py), each with
                who decided and the card it came from;
  - elsewhere:  for each *other* channel of the group, the messages written
                there since then (that channel's episodic log, read only).

"Since then" is the member's last catch-up in this channel (a mark in the
shared store), or `since` if the client sends one, or the last 7 days. The
mark moves to now after each answer, so the next catch-up starts there.

Only conversations linked on purpose (links.py) are read: a direct message is
never part of a group, so it never shows up here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..memory.sqlite import SqliteEpisodicStore
from .store import CoordinationStore

DEFAULT_WINDOW = 7 * 24 * 3600
MAX_MESSAGES = 30  # per channel; the newest ones are kept
MAX_TEXT = 400


def channel_of(conversation_id: str) -> str:
    """The client-facing channel name of a conversation id."""
    if conversation_id.startswith("web:"):
        return "web"
    if conversation_id.startswith("channel:"):
        return "discord"
    if conversation_id.startswith("slack:"):
        return "slack"
    return "other"


class CatchUp:
    def __init__(
        self,
        episodic_for: Callable[[str], SqliteEpisodicStore],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._episodic_for = episodic_for
        self._clock = clock

    async def build(
        self,
        *,
        store: CoordinationStore,
        conversation_id: str,
        group: list[str],
        member_id: int,
        since: float | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        mark_name = f"catchup:{conversation_id}:{member_id}"
        if since is None:
            mark = await store.mark(mark_name)
            since = mark[1] if mark else now - DEFAULT_WINDOW
        since = max(0.0, min(float(since), now))

        state = await store.load_state()
        decisions = [
            d.to_wire() for d in state.decisions if d.decided_at >= since
        ]

        elsewhere = []
        for other in group:
            if other == conversation_id:
                continue
            records = await self._episodic_for(other).records_between(other, since, now)
            messages = []
            for r in records:
                if r.content.strip():
                    messages.append({"speaker": r.speaker or "member", "text": _clip(r.content), "at": r.timestamp})
                if r.assistant_output and r.assistant_output.strip():
                    messages.append({"speaker": "coordinator", "text": _clip(r.assistant_output), "at": r.timestamp})
            elsewhere.append({
                "channel": channel_of(other),
                "messages": messages[-MAX_MESSAGES:],
                "total": len(messages),
            })

        await store.save_mark(mark_name, 0, now)
        return {
            "since": since,
            "until": now,
            "linked": len(group) > 1,
            "decisions": decisions,
            "elsewhere": elsewhere,
        }


def summary_line(catchup: dict[str, Any]) -> str:
    """The notice's one line: what is in the catch-up, in plain words."""
    if not catchup["linked"]:
        # How to link is told by each channel in its own words (it knows the code).
        return "This group is not linked to another channel yet."
    parts = []
    n = len(catchup["decisions"])
    if n:
        parts.append(f"{n} decision{'s' if n != 1 else ''}")
    for place in catchup["elsewhere"]:
        if place["total"]:
            m = place["total"]
            parts.append(f"{m} message{'s' if m != 1 else ''} on {place['channel'].capitalize()}")
    if not parts:
        return "Nothing new in the other channels since your last catch-up."
    return "Since your last catch-up: " + ", ".join(parts) + "."


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
