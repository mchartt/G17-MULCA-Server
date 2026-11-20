"""
Reflection (Matteo, 20/11): how the group worked in one week, group-level
only. Facts come from the store and the chat logs; only the topics come from
the model, once per day at most, never naming or ranking anyone.
"""

from __future__ import annotations

from datetime import date, datetime

import aiosqlite

from muca_server.cognition.coordination.reflection import week_of
from muca_server.cognition.memory.sqlite import db_path_for
from muca_server.protocol import CoordinatorNoticeMessage

from .conftest import drain
from .test_coordinator_links import DISCORD, DISCORD_ROSTER, WEB, Group, command, open_web_space, say

TOPICS = {"topics": [{"title": "Lab booking", "summary": "The group agreed to book the lab for Tuesday at 17:00."}]}


def at(day: int, hour: int = 10) -> float:
    return datetime(2026, 11, day, hour).timestamp()


async def reflection(router, queue, **args):
    await router.route_coordinator(command("reflection", 100_000_001, **args))
    await router.wait_for_background()
    return [m for m in await drain(queue) if isinstance(m, CoordinatorNoticeMessage)][-1]


async def plan(tmp_path, web, web_q):
    """Three tasks: one done on Tuesday, one due Monday, one made the week before (written directly:
    adding them by command would also ask the model for likely dependencies)."""
    async with aiosqlite.connect(db_path_for(WEB, base_dir=tmp_path)) as db:
        rows = (
            ("t1", "Write the survey", None, "done", at(16), at(17), 100),
            ("t2", "Book the lab", "2026-11-16", "todo", at(16), None, 0),
            ("t3", "Old task", None, "todo", at(10), None, 0),
        )
        await db.executemany(
            "INSERT INTO coord_task (task_id, title, due_date, status, created_at, done_at, progress) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await db.execute(
            "INSERT INTO coord_milestone VALUES ('m1', 'Prototype demo', '2026-11-25', 0)"
        )
        await db.commit()


def test_a_week_starts_on_monday():
    assert week_of(date(2026, 11, 20)) == date(2026, 11, 16)
    assert week_of(date(2026, 11, 16)) == date(2026, 11, 16)


async def test_the_week_in_facts_and_topics_for_the_whole_group(tmp_path):
    group = Group(tmp_path, TOPICS)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await drain(discord_q)
    await drain(web_q)
    await plan(tmp_path, web, web_q)
    for i in range(4):
        await say(tmp_path, DISCORD, "Chiara", f"Shall we book the lab for Tuesday? ({i})", at(17, 9 + i))
    for i in range(3):
        await say(tmp_path, WEB, "Ada", f"Tuesday 17:00 works for me ({i})", at(17, 14 + i))
    await say(tmp_path, WEB, "Ada", "last week, not this one", at(12))

    notice = await reflection(web, web_q, week="2026-11-18")
    week = notice.reflection
    assert notice.ok and week["week"] == "2026-11-16" and week["until"] == "2026-11-22" and week["current"]
    assert [t["title"] for t in week["done"]] == ["Write the survey"]
    assert [t["title"] for t in week["added"]] == ["Book the lab"]
    assert week["slipped"] == []  # today (16/11) is the Monday: nothing is late yet
    assert [m["title"] for m in week["upcoming"]] == ["Prototype demo"]
    assert {(t["channel"], t["messages"]) for t in week["talk"]} == {("web", 3), ("discord", 4)}
    assert week["progress"] == {"done": 1, "total": 3}
    assert week["topics"] == TOPICS["topics"]
    # Day by day, for the charts: 7 days, the group's messages (both channels) on Tuesday 17th, counts only.
    assert [d["day"] for d in week["days"]][0] == "2026-11-16" and len(week["days"]) == 7
    assert week["days"][1]["messages"] == 7 and sum(d["messages"] for d in week["days"]) == 7
    assert sum(d["done"] for d in week["days"]) == 1
    assert all(set(d) == {"day", "messages", "done"} for d in week["days"])

    # The model saw the week's chat of both channels, and nothing from the week before.
    (call,) = group.writer._client.calls if hasattr(group.writer, "_client") else [None]
    prompt = call["messages"][1]["content"] if call else ""
    assert "Shall we book the lab" in prompt and "Tuesday 17:00 works" in prompt
    assert "last week, not this one" not in prompt
    system = call["messages"][0]["content"] if call else ""
    assert "never name who talked most" in system


async def test_topics_are_kept_for_the_day_and_a_quiet_week_asks_no_model(tmp_path):
    group = Group(tmp_path, TOPICS)
    web, web_q = await open_web_space(group)
    for i in range(6):
        await say(tmp_path, WEB, "Ada", f"message {i} about the survey", at(17, 9 + i))
    first = await reflection(web, web_q)
    second = await reflection(web, web_q)
    assert first.reflection["topics"] == second.reflection["topics"] == TOPICS["topics"]
    assert len(group.writer._client.calls) == 1  # the second one used the day's cache

    quiet = await reflection(web, web_q, week="2026-11-02")
    assert quiet.reflection["topics"] == [] and quiet.reflection["done"] == []
    assert len(group.writer._client.calls) == 1  # too few messages: no model call


async def test_a_bad_week_is_refused(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    notice = await reflection(web, web_q, week="next friday")
    assert not notice.ok and notice.reflection is None
