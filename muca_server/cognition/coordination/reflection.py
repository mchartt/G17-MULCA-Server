"""
Reflection: how the group worked in one week (Matteo, 20/11), for the
Reflection page and the "reflection" command.

The facts are computed in code from what is on disk, so they are true:

  done       tasks finished in the week (the store's done_at)
  added      tasks made in the week (created_at), not yet done
  decisions  decisions closed in the week, with their reason
  slipped    open tasks whose date fell in the week, before today
  upcoming   milestones in the next two weeks
  talk       how many messages each channel of the group had in the week
  progress   tasks done / all tasks, now

Only `topics` comes from the model: up to five things the group talked about,
each in one sentence, read from that week's chat in every linked channel. It
is asked only when there are enough messages, and it never names who talked
most or ranks anyone (the privacy promise on the login page: Cadence never
scores or ranks people). Everything is about the group, never per person.

Topics are kept in coord_setting ("reflection:<monday>") with the day they
were made: a past week is summed up once, the current week at most once a day.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from .catchup import channel_of
from .models import TaskStatus
from .store import CoordinationStore

MIN_MESSAGES_FOR_TOPICS = 6
MAX_LINES = 300
MAX_LINE = 240
UPCOMING_DAYS = 14


def week_of(day: date) -> date:
    """The Monday of the week `day` falls in."""
    return day - timedelta(days=day.weekday())


def _at(day: date) -> float:
    return datetime(day.year, day.month, day.day).timestamp()


async def build_reflection(
    *,
    store: CoordinationStore,
    group: list[str],
    monday: date,
    today: date,
    read_channel: Callable[[str, float, float], Awaitable[list[Any]]],
    draft_topics: Callable[[list[str]], Awaitable[list[dict] | None]] | None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    start_day, end_day = monday, monday + timedelta(days=7)
    start, end = _at(start_day), _at(end_day)
    state = await store.load_state()
    names = {m.member_id: m.display_name for m in state.members}

    def task(t) -> dict[str, Any]:
        return {"task_id": t.task_id, "title": t.title, "owner_id": t.owner_id, "due_date": t.due_date.isoformat() if t.due_date else None}

    done = [task(t) | {"at": t.done_at} for t in state.tasks if t.done_at is not None and start <= t.done_at < end]
    added = [
        task(t) | {"at": t.created_at}
        for t in state.tasks
        if t.created_at is not None and start <= t.created_at < end and t.status != TaskStatus.DONE
    ]
    decisions = [
        {"decision_id": d.decision_id, "question": d.question, "choice": d.choice, "reason": d.reason, "at": d.decided_at}
        for d in state.decisions
        if start <= d.decided_at < end
    ]
    slipped = [
        task(t)
        for t in state.tasks
        if t.status != TaskStatus.DONE and t.due_date is not None and start_day <= t.due_date < min(end_day, today)
    ]
    upcoming = [
        {"milestone_id": m.milestone_id, "title": m.title, "due_date": m.due_date.isoformat()}
        for m in state.milestones
        if m.due_date is not None and today <= m.due_date <= today + timedelta(days=UPCOMING_DAYS)
    ]
    total = len(state.tasks)
    finished = sum(1 for t in state.tasks if t.status == TaskStatus.DONE)

    talk: list[dict[str, Any]] = []
    lines: list[str] = []
    # The week day by day, for the page's small charts: the group's messages and
    # tasks done each day (counts only, never who).
    days = [{"day": (start_day + timedelta(days=i)).isoformat(), "messages": 0, "done": 0} for i in range(7)]

    def day_of(at: float) -> int | None:
        index = (datetime.fromtimestamp(at).date() - start_day).days
        return index if 0 <= index < 7 else None

    for t in done:
        index = day_of(t["at"])
        if index is not None:
            days[index]["done"] += 1
    for conversation_id in group:
        records = await read_channel(conversation_id, start, end)
        said = [r for r in records if r.content.strip()]
        talk.append({"channel": channel_of(conversation_id), "messages": len(said)})
        for r in said:
            index = day_of(r.timestamp)
            if index is not None:
                days[index]["messages"] += 1
        for r in said:
            text = " ".join(r.content.split())[:MAX_LINE]
            lines.append(f"{datetime.fromtimestamp(r.timestamp).strftime('%a %H:%M')} {text}")
    lines = lines[-MAX_LINES:]

    topics = await _topics(store, monday, today, lines, draft_topics)
    return {
        "week": monday.isoformat(),
        "until": (end_day - timedelta(days=1)).isoformat(),
        "current": monday <= today < end_day,
        "done": done,
        "added": added,
        "decisions": decisions,
        "slipped": slipped,
        "upcoming": upcoming,
        "talk": talk,
        "days": days,
        "progress": {"done": finished, "total": total},
        "topics": topics,
        "names": names,
    }


async def _topics(
    store: CoordinationStore,
    monday: date,
    today: date,
    lines: list[str],
    draft_topics: Callable[[list[str]], Awaitable[list[dict] | None]] | None,
) -> list[dict[str, str]]:
    """The week's topics: from the cache when still fresh, else one model call (or none)."""
    if len(lines) < MIN_MESSAGES_FOR_TOPICS or draft_topics is None:
        return []
    key = f"reflection:{monday.isoformat()}"
    cached = await store.setting(key)
    if cached:
        try:
            saved = json.loads(cached)
            past_week = monday + timedelta(days=7) <= today
            if past_week or saved.get("day") == today.isoformat():
                return list(saved.get("topics", []))
        except (ValueError, AttributeError):
            pass
    topics = await draft_topics(lines)
    if topics is None:
        return []
    await store.save_setting(key, json.dumps({"day": today.isoformat(), "topics": topics}))
    return topics
