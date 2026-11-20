"""
Slice C: a task made by hand can carry a description and the tasks it waits on.
The server checks both (a real task, never itself, never a loop), exactly as it
checks an owner or a date. By hand: no card, the model is never asked.
"""

from __future__ import annotations

from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.protocol import CoordinatorNoticeMessage, CoordinatorStateMessage

from .conftest import drain
from .test_coordinator_language_charter import make
from .test_coordinator_router import command, of_type


async def last_notice(queue):
    return of_type(await drain(queue), CoordinatorNoticeMessage)[-1]


async def add(router, queue, title, **args):
    """Add a task by hand and return its id."""
    await router.route_coordinator(command("task", add=title, **args))
    state = of_type(await drain(queue), CoordinatorStateMessage)[-1].state
    return next(t["task_id"] for t in state["tasks"] if t["title"] == title)


async def test_a_task_is_added_with_a_description_and_what_it_waits_on(store):
    router, queue, client = make(store)
    api = await add(router, queue, "Build the API")
    await router.route_coordinator(
        command("task", add="Test the API", description="  Run the contract tests\nand note failures ", depends_on=[api])
    )
    sent = await drain(queue)
    assert of_type(sent, CoordinatorNoticeMessage)[-1].ok
    wire = {t["title"]: t for t in of_type(sent, CoordinatorStateMessage)[-1].state["tasks"]}  # what the clients receive
    assert wire["Test the API"]["description"] == "Run the contract tests\nand note failures"
    assert wire["Test the API"]["depends_on"] == [api]
    tasks = {t.title: t for t in (await store.load_state()).tasks}
    assert tasks["Test the API"].description == "Run the contract tests\nand note failures"
    assert tasks["Test the API"].depends_on == [api]
    assert tasks["Build the API"].description == "" and tasks["Build the API"].depends_on == []
    assert client.calls == []  # by hand: the model is never asked


async def test_waits_on_accepts_the_number_shown_in_the_plan(store):
    router, queue, _ = make(store)
    first = await add(router, queue, "Interviews")
    await router.route_coordinator(command("task", add="Synthesis", depends_on=["1"]))
    await drain(queue)
    tasks = {t.title: t for t in (await store.load_state()).tasks}
    assert tasks["Synthesis"].depends_on == [first]


async def test_a_task_cannot_wait_on_a_stranger_or_on_itself(store):
    router, queue, _ = make(store)
    a = await add(router, queue, "A")
    for bad in (["nope"], [a, "nope"], "A", 7):
        await router.route_coordinator(command("task", add="B", depends_on=bad))
        assert not (await last_notice(queue)).ok, bad
    assert [t.title for t in (await store.load_state()).tasks] == ["A"]  # a refused add adds nothing

    await router.route_coordinator(command("task", task=a, depends_on=[a]))
    notice = await last_notice(queue)
    assert not notice.ok and "itself" in notice.text
    assert (await store.load_state()).tasks[0].depends_on == []


async def test_a_loop_is_refused_even_through_other_tasks(store):
    router, queue, _ = make(store)
    a = await add(router, queue, "A")
    b = await add(router, queue, "B", depends_on=[a])  # B waits on A
    c = await add(router, queue, "C", depends_on=[b])  # C waits on B

    await router.route_coordinator(command("task", task=b, depends_on=[a, c]))  # B would wait on C, which waits on B
    notice = await last_notice(queue)
    assert not notice.ok and "loop" in notice.text.lower()
    await router.route_coordinator(command("task", task=a, depends_on=[c]))  # A -> C -> B -> A
    assert not (await last_notice(queue)).ok

    tasks = {t.title: t for t in (await store.load_state()).tasks}
    assert (tasks["A"].depends_on, tasks["B"].depends_on, tasks["C"].depends_on) == ([], [a], [b])  # nothing changed


async def test_description_and_waits_on_are_changed_and_cleared_by_hand(store):
    router, queue, _ = make(store)
    a = await add(router, queue, "A")
    b = await add(router, queue, "B")
    await router.route_coordinator(command("task", task=b, description="Why it matters", depends_on=[a]))
    assert (await last_notice(queue)).text == "“B” has a new description, waits on 1 task."
    saved = {t.title: t for t in (await store.load_state()).tasks}["B"]
    assert (saved.description, saved.depends_on) == ("Why it matters", [a])

    await router.route_coordinator(command("task", task=b, description="", depends_on=[]))
    assert (await last_notice(queue)).text == "“B” has no description, waits on nothing."
    saved = {t.title: t for t in (await store.load_state()).tasks}["B"]
    assert (saved.description, saved.depends_on) == ("", [])


async def test_a_description_that_is_too_long_changes_nothing(store):
    router, queue, _ = make(store)
    a = await add(router, queue, "A")
    await router.route_coordinator(command("task", task=a, description="x" * 2001))
    assert not (await last_notice(queue)).ok
    await router.route_coordinator(command("task", add="B", description="x" * 2001))
    assert not (await last_notice(queue)).ok
    state = await store.load_state()
    assert [t.title for t in state.tasks] == ["A"] and state.tasks[0].description == ""


async def test_a_non_member_cannot_set_details(store):
    router, queue, _ = make(store)
    a = await add(router, queue, "A")
    await router.route_coordinator(command("task", member=77, task=a, description="Mine", depends_on=[a]))
    assert not (await last_notice(queue)).ok
    assert (await store.load_state()).tasks[0].description == ""


async def test_removing_a_task_frees_the_tasks_that_waited_on_it(store):
    router, queue, _ = make(store)
    a = await add(router, queue, "A")
    await add(router, queue, "B", depends_on=[a])
    await router.route_coordinator(command("task", task=a, remove=True))
    await drain(queue)
    (b,) = (await store.load_state()).tasks
    assert b.depends_on == []


async def test_a_file_made_before_descriptions_existed_still_opens(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE coord_task (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, owner_id INTEGER, due_date TEXT, status TEXT NOT NULL DEFAULT 'todo')")
    db.execute("INSERT INTO coord_task (task_id, title) VALUES ('t1', 'Old task')")
    db.commit()
    db.close()
    (task,) = (await CoordinationStore(path).load_state()).tasks
    assert (task.title, task.description) == ("Old task", "")
