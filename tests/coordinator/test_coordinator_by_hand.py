"""
The plan edited by hand: tasks and milestones a member adds, changes or
removes; the planning list with owners already chosen; the background a
member writes about themselves. A member's own action: no card (except the
plan list), and never a model call when nothing is left to suggest.
"""

from __future__ import annotations

from datetime import date

from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.protocol import (
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
)

from .conftest import drain
from .test_coordinator_language_charter import make
from .test_coordinator_router import click, command, of_type


async def last_notice(queue):
    return of_type(await drain(queue), CoordinatorNoticeMessage)[-1]


async def test_a_member_adds_a_task_gives_it_an_owner_a_date_and_closes_it(store):
    router, queue, client = make(store)
    await router.route_coordinator(command("task", add="  Write   the report ", owner=2, due="2026-12-01"))
    sent = await drain(queue)
    assert of_type(sent, CoordinatorNoticeMessage)[0].text == "Task added: “Write the report” (Bruno)."
    (task,) = of_type(sent, CoordinatorStateMessage)[0].state["tasks"]
    assert (task["title"], task["owner_id"], task["due_date"], task["status"]) == ("Write the report", 2, "2026-12-01", "todo")

    await router.route_coordinator(command("task", task=task["task_id"], owner=""))  # nobody
    assert (await last_notice(queue)).text == "“Write the report” has no owner."
    await router.route_coordinator(command("task", task="1", owner=3, status="done", due=""))  # by its number
    assert (await last_notice(queue)).text == "“Write the report” is now done, is Chiara's, has no date."
    (saved,) = (await store.load_state()).tasks
    assert (saved.owner_id, saved.status.value, saved.due_date) == (3, "done", None)
    assert client.calls == []  # by hand: the model is never asked


async def test_a_wrong_owner_date_or_status_changes_nothing(store):
    router, queue, _ = make(store)
    await router.route_coordinator(command("task", add="Slides"))
    await drain(queue)
    for bad in ({"owner": 77}, {"owner": "bruno"}, {"due": "next week"}, {"status": "finished"}, {"title": " "}, {}):
        await router.route_coordinator(command("task", task="1", **bad))
        assert not (await last_notice(queue)).ok, bad
    (task,) = (await store.load_state()).tasks
    assert (task.title, task.owner_id, task.due_date, task.status.value) == ("Slides", None, None, "todo")
    await router.route_coordinator(command("task", add="x" * 201))
    assert not (await last_notice(queue)).ok


async def test_a_member_renames_and_removes_a_task(store):
    router, queue, _ = make(store)
    await router.route_coordinator(command("task", add="Sldies"))
    await router.route_coordinator(command("task", task="1", title="Slides"))
    assert (await last_notice(queue)).text == "“Slides” was renamed."
    await router.route_coordinator(command("task", task="1", remove=True))
    assert (await last_notice(queue)).text == "“Slides” was removed from the plan."
    assert (await store.load_state()).tasks == []
    await router.route_coordinator(command("task", task="1", status="done"))
    assert (await last_notice(queue)).text == "No such task in the plan."


async def test_a_non_member_cannot_edit_the_plan_by_hand(store):
    router, queue, _ = make(store)
    await router.route_coordinator(command("task", member=77, add="Mine"))
    await router.route_coordinator(command("milestone", member=77, add="Mine"))
    assert all(not n.ok for n in of_type(await drain(queue), CoordinatorNoticeMessage))
    state = await store.load_state()
    assert state.tasks == [] and state.milestones == []


async def test_milestones_are_added_changed_and_removed_by_hand(store):
    router, queue, client = make(store)
    await router.route_coordinator(command("milestone", add="First draft", due="2026-11-25"))
    await router.route_coordinator(command("milestone", add="Demo"))
    first, demo = (await store.load_state()).milestones  # in the order they were added
    assert (first.title, first.due_date, demo.due_date) == ("First draft", date(2026, 11, 25), None)

    await router.route_coordinator(command("milestone", milestone=demo.milestone_id, due="2026-12-02", title="Demo Madness"))
    assert (await last_notice(queue)).text == "Milestone “Demo Madness” was updated."
    await router.route_coordinator(command("milestone", milestone=first.milestone_id, remove=True))
    (only,) = (await store.load_state()).milestones
    assert (only.title, only.due_date) == ("Demo Madness", date(2026, 12, 2))

    for bad in ({"milestone": "nope", "title": "X"}, {"milestone": only.milestone_id}, {"add": ""}, {"add": "Y", "due": "soon"}):
        await router.route_coordinator(command("milestone", **bad))
        assert not (await last_notice(queue)).ok, bad
    assert client.calls == []


async def test_a_plan_list_with_every_owner_chosen_needs_no_model(store):
    router, queue, client = make(store)
    await router.route_coordinator(command("planning", items=[
        {"title": "Server API", "owner_id": 1, "due_date": "2026-11-30"},
        {"title": "UI screens", "owner_id": "2"},
    ]))
    await router.wait_for_background()
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)
    assert client.calls == []
    assert card.proposal["rationale"] == "Ada listed the tasks with their owners."
    assert [(i["title"], i["owner_id"], i["due_date"]) for i in card.proposal["payload"]["items"]] == [
        ("Server API", 1, "2026-11-30"), ("UI screens", 2, None)]
    assert (await store.load_state()).tasks == []  # a card: nothing before a member confirms

    await router.route_coordinator(click(card.proposal["proposal_id"], member=3))
    assert sorted((t.title, t.owner_id) for t in (await store.load_state()).tasks) == [("Server API", 1), ("UI screens", 2)]


async def test_only_the_tasks_without_an_owner_are_sent_to_the_model(store):
    suggested = {"items": [{"title": "User test", "owner_id": 3, "reason": "Research skills", "due_date": None}],
                 "rationale": "Stepping in because one task has no owner."}
    router, queue, client = make(store, suggested)
    await router.route_coordinator(command("planning", items=[
        {"title": "Server API", "owner_id": 1},
        {"title": "User test"},
        {"title": "   "},
    ]))
    await router.wait_for_background()
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)
    (call,) = client.calls
    asked = call["messages"][1]["content"]
    assert "<tasks>\nUser test\n</tasks>" in asked and "Server API" not in asked
    items = card.proposal["payload"]["items"]
    assert [(i["title"], i["owner_id"], i["reason"]) for i in items] == [
        ("Server API", 1, "Chosen by the group."), ("User test", 3, "Research skills")]


async def test_a_plan_list_with_a_stranger_or_a_past_date_is_refused(store):
    router, queue, client = make(store)
    for items in ([{"title": "X", "owner_id": 99}], [{"title": "X", "owner_id": 1, "due_date": "2020-01-01"}], [{"title": ""}]):
        await router.route_coordinator(command("planning", items=items))
        await router.wait_for_background()
        sent = await drain(queue)
        assert not of_type(sent, CoordinatorProposalMessage)
        assert not of_type(sent, CoordinatorNoticeMessage)[-1].ok
    assert client.calls == []


async def test_the_background_a_member_writes_reaches_the_model_with_discipline_and_skills(store):
    suggested = {"items": [{"title": "Interviews", "owner_id": 2, "reason": "Did user research before", "due_date": None}],
                 "rationale": "Stepping in because you listed a task."}
    router, queue, client = make(store, suggested)
    await router.route_coordinator(command(
        "profile", member=2, discipline="Design", skills=["figma"], weekly_hours=6,
        background="Two years of user research\nat a museum.",
    ))
    state = of_type(await drain(queue), CoordinatorStateMessage)[0].state
    assert state["members"][0]["background"] == "Two years of user research\nat a museum."
    await router.route_coordinator(command("planning", text="Interviews"))
    await router.wait_for_background()
    asked = client.calls[0]["messages"][1]["content"]
    assert "- id 2: Bruno | discipline: Design | skills: figma | weekly hours: 6 | background: Two years of user research at a museum." in asked


async def test_a_file_made_before_backgrounds_existed_still_opens(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE coord_member (member_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, discipline TEXT NOT NULL DEFAULT '', skills_json TEXT NOT NULL DEFAULT '[]', weekly_hours INTEGER)")
    db.execute("INSERT INTO coord_member (member_id, display_name, discipline) VALUES (1, 'Ada', 'CS')")
    db.commit()
    db.close()
    (member,) = (await CoordinationStore(path).load_state()).members
    assert (member.display_name, member.discipline, member.background) == ("Ada", "CS", "")
