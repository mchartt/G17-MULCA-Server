"""
Catch up (CS12): what a member missed, in every channel of their group.

A member who comes back (e.g. opens the web space on Monday after the group
talked on Discord on Thursday) asks "what did I miss?". The answer is built
from what is on disk, with no model call, so it never invents anything:

  - decisions:  the shared decision log since then (store.py), each with
                who decided and the card it came from;
  - here:       the messages written in this channel since then (Matteo,
                20/11: the group chat too, not only the other channels);
  - elsewhere:  for each *other* channel of the group, the messages written
                there since then (that channel's episodic log, read only);
  - tasks_done / tasks_added: what changed in the plan since then (the
                store's done_at and created_at).

"Since then" is the member's last catch-up in this channel (a mark in the
shared store), or `since` if the client sends one, or the last 7 days. The
mark moves to now after each answer, so the next catch-up starts there;
`move_mark=False` lets a client show the catch-up first and move the mark
only when the member closes it (command catchup with args.peek, then
args.seen).

Only conversations linked on purpose (links.py) are read: a direct message is
never part of a group, so it never shows up here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..memory.records import EpisodicRecord
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
        move_mark: bool = True,
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

        here = await self._channel(conversation_id, since, now)
        elsewhere = [await self._channel(other, since, now) for other in group if other != conversation_id]
        tasks_done = [
            {"task_id": t.task_id, "title": t.title, "owner_id": t.owner_id, "at": t.done_at}
            for t in state.tasks
            if t.done_at is not None and t.done_at >= since
        ]
        tasks_added = [
            {"task_id": t.task_id, "title": t.title, "owner_id": t.owner_id, "at": t.created_at}
            for t in state.tasks
            if t.created_at is not None and t.created_at >= since and t.done_at is None
        ]

        if move_mark:
            await store.save_mark(mark_name, 0, now)
        return {
            "since": since,
            "until": now,
            "linked": len(group) > 1,
            "decisions": decisions,
            "here": here,
            "elsewhere": elsewhere,
            "tasks_done": tasks_done,
            "tasks_added": tasks_added,
        }

    async def seen(self, *, store: CoordinationStore, conversation_id: str, member_id: int) -> None:
        """The member closed the catch-up: the next one starts now."""
        await store.save_mark(f"catchup:{conversation_id}:{member_id}", 0, self._clock())

    async def read(self, conversation_id: str, since: float, until: float) -> list:
        """One channel's chat between two times (read only): also used by reflection.py."""
        return await self._episodic_for(conversation_id).records_between(conversation_id, since, until)

    async def remember(self, conversation_id: str, speaker: str, said: str, answer: str) -> None:
        """A member's words to Cadence and its answer, kept in the chat's log (asked by command, not by user_text)."""
        await self._episodic_for(conversation_id).log(
            EpisodicRecord(
                timestamp=self._clock(),
                conversation_id=conversation_id,
                intervention_path="strategy",
                strategy_id="coordinator_ask",
                speaker=speaker,
                content=said,
                assistant_output=answer,
            )
        )

    async def recent_lines(self, conversation_id: str, *, window: float = 24 * 3600, limit: int = 30) -> list[str]:
        """The last lines of one channel's chat as "Name: text", Cadence's answers included."""
        now = self._clock()
        lines: list[str] = []
        for r in await self.read(conversation_id, now - window, now):
            if r.content.strip():
                lines.append(f"{r.speaker or 'member'}: {_clip(r.content)}")
            if r.assistant_output and r.assistant_output.strip():
                lines.append(f"Cadence: {_clip(r.assistant_output)}")
        return lines[-limit:]

    async def _channel(self, conversation_id: str, since: float, now: float) -> dict[str, Any]:
        records = await self.read(conversation_id, since, now)
        messages = []
        for r in records:
            if r.content.strip():
                messages.append({"speaker": r.speaker or "member", "text": _clip(r.content), "at": r.timestamp})
            if r.assistant_output and r.assistant_output.strip():
                messages.append({"speaker": "coordinator", "text": _clip(r.assistant_output), "at": r.timestamp})
        return {
            "channel": channel_of(conversation_id),
            "messages": messages[-MAX_MESSAGES:],
            "total": len(messages),
        }


def summary_line(catchup: dict[str, Any]) -> str:
    """The notice's one line: what is in the catch-up, in plain words."""
    parts = []
    n = len(catchup["decisions"])
    if n:
        parts.append(f"{n} decision{'s' if n != 1 else ''}")
    done = len(catchup.get("tasks_done", []))
    if done:
        parts.append(f"{done} task{'s' if done != 1 else ''} done")
    added = len(catchup.get("tasks_added", []))
    if added:
        parts.append(f"{added} new task{'s' if added != 1 else ''}")
    here = (catchup.get("here") or {}).get("total", 0)
    if here:
        parts.append(f"{here} message{'s' if here != 1 else ''} here")
    for place in catchup["elsewhere"]:
        if place["total"]:
            m = place["total"]
            parts.append(f"{m} message{'s' if m != 1 else ''} on {place['channel'].capitalize()}")
    if parts:
        return "Since your last catch-up: " + ", ".join(parts) + "."
    if not catchup["linked"]:
        # How to link is told by each channel in its own words (it knows the code).
        return "Nothing new since your last catch-up. This group is not linked to another channel yet."
    return "Nothing new since your last catch-up."


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
