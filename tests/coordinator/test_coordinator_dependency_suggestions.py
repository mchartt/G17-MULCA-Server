"""
Matteo, 20/11: when a task is added, Cadence reasons on whether it likely
waits on another open task and, if so, posts a CS7 "possible" card the group
confirms or not (the model proposes, a member decides). Confirming records the
dependency without marking the task blocked. Do not fire: the member already
said what it waits on, fewer than two open tasks, a card on the same task is
waiting, the topic was silenced with "Not now", a pair already recorded.
"""

from __future__ import annotations

import time

import aiosqlite

from muca_server.protocol import CoordinatorProposalMessage

from .conftest import drain
from .test_coordinator_language_charter import make
from .test_coordinator_router import click, command, of_type
from .test_coordinator_task_details import add

RATIONALE = "Stepping in because a new task seems to need another one first."


def links(*pairs):
    return {
        "links": [{"task_id": a, "waits_on": b, "reason": "Tests need the API to exist."} for a, b in pairs],
        "rationale": RATIONALE,
    }


def answer_with_new_id(client, store, waits_on, title="Test the API"):
    """The new task's id exists only once it is added: the fake model answers with it when called."""
    original = client._create

    async def answer(**kwargs):
        new_id = next(t.task_id for t in (await store.load_state()).tasks if t.title == title)
        client.answers.append(links((new_id, waits_on)))
        return await original(**kwargs)

    client.chat.completions.create = answer


async def cards(router, queue):
    await router.wait_for_background()
    return [m.proposal for m in of_type(await drain(queue), CoordinatorProposalMessage)]


async def two_tasks(router, queue):
    api = await add(router, queue, "Build the API")
    await router.wait_for_background()  # one open task: nothing to compare, no model call
    return api


async def test_a_new_task_that_needs_another_gets_a_possible_dependency_card(store):
    router, queue, client = make(store)
    api = await two_tasks(router, queue)
    assert client.calls == []
    answer_with_new_id(client, store, api)
    await router.route_coordinator(command("task", add="Test the API"))
    tests_id = next(t.task_id for t in (await store.load_state()).tasks if t.title == "Test the API")

    (card,) = await cards(router, queue)
    assert card["kind"] == "blocker" and card["status"] == "pending"
    payload = card["payload"]
    assert payload["certainty"] == "possible"
    assert payload["task_id"] == tests_id and payload["depends_on_task_id"] == api
    assert [e["text"] for e in payload["context"]["evidence"]] == ["New task: Test the API", "Open task: Build the API"]
    assert "Test the API" in client.calls[0]["messages"][1]["content"]

    # Confirming records the dependency; nobody said it is blocked, so it is not.
    await router.route_coordinator(click(card["proposal_id"], "confirm"))
    await drain(queue)
    task = next(t for t in (await store.load_state()).tasks if t.task_id == tests_id)
    assert task.depends_on == [api] and task.status.value == "todo"


async def test_no_card_when_the_member_said_what_it_waits_on(store):
    router, queue, client = make(store)
    api = await two_tasks(router, queue)
    await router.route_coordinator(command("task", add="Test the API", depends_on=[api]))
    assert await cards(router, queue) == []
    assert client.calls == []


async def test_no_card_when_the_model_finds_no_clear_dependency(store):
    router, queue, client = make(store, links())
    await two_tasks(router, queue)
    await router.route_coordinator(command("task", add="Book the room"))
    assert await cards(router, queue) == []
    assert len(client.calls) == 1


async def test_a_link_to_a_task_that_does_not_exist_is_never_posted(store):
    """An invented id: one retry, then silence (project rule 0, never invent)."""
    router, queue, client = make(store, links(("x", "nope")), links(("x", "nope")))
    await two_tasks(router, queue)
    await router.route_coordinator(command("task", add="Test the API"))
    assert await cards(router, queue) == []
    assert len(client.calls) == 2


async def test_a_silenced_topic_gets_no_card(store):
    router, queue, client = make(store)
    api = await two_tasks(router, queue)
    # What "Not now" on an earlier card about this task wrote (plan_writes.add_snooze).
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "INSERT INTO coord_snooze VALUES ('cs7_blockers_dependencies', 'test the api', ?)", (time.time() + 600,)
        )
        await db.commit()
    answer_with_new_id(client, store, api)
    await router.route_coordinator(command("task", add="Test the API"))
    assert await cards(router, queue) == []
    assert len(client.calls) == 1  # it looked, then stayed silent
