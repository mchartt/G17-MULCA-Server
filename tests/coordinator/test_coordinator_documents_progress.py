"""
Matteo, 20/11: Cadence keeps the documents the group gives it (Project
settings > Documents, and the setup desk's kickoff) as text it can read later;
a task carries its progress and when it was made and finished, so the Tasks
page shows progress and Reflection can count "done this week". By hand: no
card, the model is never asked (except the kickoff's own charter draft).
"""

from __future__ import annotations

import time

from muca_server.cognition.coordination.models import MAX_DOCUMENT_CHARS, MAX_DOCUMENTS
from muca_server.protocol import CoordinatorNoticeMessage, CoordinatorStateMessage

from .conftest import drain
from .test_coordinator_language_charter import GOOD_CHARTER, make
from .test_coordinator_router import command, of_type
from .test_coordinator_task_details import add, last_notice


# ----------------------------------------------------------------------
# Progress and times
# ----------------------------------------------------------------------


async def test_a_new_task_knows_when_it_was_made_and_starts_at_zero(store):
    router, queue, _ = make(store)
    before = time.time()
    await add(router, queue, "Interviews")
    task = (await store.load_state()).tasks[0]
    assert task.progress == 0 and task.done_at is None
    assert task.created_at is not None and before - 5 <= task.created_at <= time.time() + 5


async def test_progress_is_set_by_hand_and_checked(store):
    router, queue, client = make(store)
    task_id = await add(router, queue, "Survey")
    await router.route_coordinator(command("task", task=task_id, progress=40))
    sent = await drain(queue)
    notice = of_type(sent, CoordinatorNoticeMessage)[-1]
    assert notice.ok and "40%" in notice.text
    assert of_type(sent, CoordinatorStateMessage)[-1].state["tasks"][0]["progress"] == 40  # what clients receive
    for bad in (-1, 101, "half", None):
        await router.route_coordinator(command("task", task=task_id, progress=bad))
        assert not (await last_notice(queue)).ok, bad
    assert (await store.load_state()).tasks[0].progress == 40
    assert client.calls == []


async def test_done_sets_the_time_and_full_progress_and_reopening_clears_the_time(store):
    router, queue, _ = make(store)
    task_id = await add(router, queue, "Report")
    await router.route_coordinator(command("task", task=task_id, status="done"))
    await drain(queue)
    task = (await store.load_state()).tasks[0]
    assert task.done_at is not None and task.progress == 100

    await router.route_coordinator(command("task", task=task_id, status="doing"))
    await drain(queue)
    task = (await store.load_state()).tasks[0]
    assert task.done_at is None


async def test_an_old_file_gets_progress_and_times_without_losing_its_tasks(tmp_path):
    """A group file made before 20/11: the columns are added, old tasks keep everything, with no times."""
    import aiosqlite

    from muca_server.cognition.coordination.store import CoordinationStore

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE coord_task (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, owner_id INTEGER, "
            "due_date TEXT, status TEXT NOT NULL DEFAULT 'todo')"
        )
        await db.execute("INSERT INTO coord_task VALUES ('t1', 'Old task', NULL, NULL, 'todo')")
        await db.commit()
    store = CoordinationStore(path)
    task = (await store.load_state()).tasks[0]
    assert task.title == "Old task" and task.progress == 0 and task.created_at is None
    await store.edit_task("t1", {"status": "done"})
    assert (await store.load_state()).tasks[0].done_at is not None


# ----------------------------------------------------------------------
# Documents
# ----------------------------------------------------------------------


async def test_a_document_is_kept_listed_without_its_text_and_removed(store):
    router, queue, client = make(store)
    await router.route_coordinator(command("document", add={"name": "  Brief.pdf ", "text": "Deliver a prototype by 2 December."}))
    sent = await drain(queue)
    notice = of_type(sent, CoordinatorNoticeMessage)[-1]
    assert notice.ok and "Brief.pdf" in notice.text
    listed = of_type(sent, CoordinatorStateMessage)[-1].state["documents"]
    assert [d["name"] for d in listed] == ["Brief.pdf"]
    assert listed[0]["chars"] == len("Deliver a prototype by 2 December.")
    assert "text" not in listed[0]  # the panel lists it; the text stays on the server
    assert await store.document_texts() == [("Brief.pdf", "Deliver a prototype by 2 December.")]

    await router.route_coordinator(command("document", remove=listed[0]["document_id"]))
    sent = await drain(queue)
    assert of_type(sent, CoordinatorNoticeMessage)[-1].ok
    assert of_type(sent, CoordinatorStateMessage)[-1].state["documents"] == []
    assert await store.document_texts() == []
    assert client.calls == []


async def test_a_document_needs_a_name_and_text_and_is_trimmed_to_the_limit(store):
    router, queue, _ = make(store)
    for bad in ({"name": "", "text": "x"}, {"name": "A", "text": "   "}, "not a dict"):
        await router.route_coordinator(command("document", add=bad))
        assert not (await last_notice(queue)).ok, bad
    await router.route_coordinator(command("document", remove="nope"))
    assert not (await last_notice(queue)).ok
    await router.route_coordinator(command("document", add={"name": "Long", "text": "a" * (MAX_DOCUMENT_CHARS + 50)}))
    await drain(queue)
    assert len((await store.document_texts())[0][1]) == MAX_DOCUMENT_CHARS


async def test_a_project_keeps_a_bounded_number_of_documents(store):
    router, queue, _ = make(store)
    for i in range(MAX_DOCUMENTS):
        await router.route_coordinator(command("document", add={"name": f"Doc {i}", "text": "text"}))
    await drain(queue)
    await router.route_coordinator(command("document", add={"name": "One too many", "text": "text"}))
    notice = await last_notice(queue)
    assert not notice.ok and "remove one" in notice.text
    assert len(await store.document_texts()) == MAX_DOCUMENTS


async def test_kickoff_keeps_the_description_and_the_documents_of_the_setup_desk(store):
    router, queue, _ = make(store, GOOD_CHARTER)
    await router.route_coordinator(
        command(
            "kickoff",
            text="AUI prototype.\n\nassignment.txt:\nDeliver by 2 December.",
            description="AUI prototype.",
            documents=[{"name": "assignment.txt", "text": "Deliver by 2 December."}, "junk"],
        )
    )
    await router.wait_for_background()
    assert await store.document_texts() == [
        ("Project description", "AUI prototype."),
        ("assignment.txt", "Deliver by 2 December."),
    ]


async def test_an_old_kickoff_with_text_only_keeps_no_document(store):
    """Clients that send only the composed text (Discord /coordinator kickoff) keep working as before."""
    router, queue, _ = make(store, GOOD_CHARTER)
    await router.route_coordinator(command("kickoff", text="Assignment text"))
    await router.wait_for_background()
    assert await store.document_texts() == []
