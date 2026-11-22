"""
Asked in words to update a task or to add several tasks, Cadence drafts a
card instead of answering "non posso" (20/11 test space: "segna il prototipo
al 50%" got "Non posso modificare l'avanzamento"; three tasks at once got "I
can't save them directly"). As always, only a member's click changes the plan.
"""

from __future__ import annotations

from datetime import date

import pytest

from muca_server.cognition.coordination.ask_action import ask_action
from muca_server.cognition.coordination.models import TaskStatus
from muca_server.protocol import CoordinatorNoticeMessage, parse_incoming_message

from .conftest import drain
from .test_coordinator_conversational import ADA, BRUNO, answer, ask, by_id, task
from .test_coordinator_language_charter import make
from .test_coordinator_router import click, of_type

PROTOTYPE = "Prototipo Figma"


def choose(proposal_id, option=0, member=ADA):
    return parse_incoming_message(
        {"event": "proposal_response", "proposalId": proposal_id, "action": "choose", "option": option,
         "memberId": member, "channel": "web", "requestId": "c1"}
    )


async def test_asked_to_mark_progress_cadence_drafts_a_card_and_the_click_saves_it(store):
    proto = await task(store, PROTOTYPE, owner=ADA)
    router, queue, _ = make(store, answer(
        "Segno il 50%: conferma la card qui sotto.", {"kind": "update_task", "task_id": proto, "progress": 50},
    ))
    notice, (card,) = await ask(router, queue, "Cadence, ho fatto metà del prototipo, segnalo")
    assert notice.ok and "conferma la card" in notice.text
    assert card["kind"] == "checkin" and card["status"] == "pending"
    (option,) = card["payload"]["options"]
    assert option["task_id"] == proto and option["new_progress"] == 50
    assert option["label"] == f"«{PROTOTYPE}»: avanzamento 50%"
    assert "ho fatto metà del prototipo" in card["payload"]["findings"][0]
    assert (await by_id(store, proto)).progress == 0  # nothing changed before the click
    # Live test 20/11: a requested update is titled as such, and its reason
    # is not the chat answer repeated (that came out twice, card and message).
    assert card["payload"]["requested"] is True
    assert card["rationale"].startswith("Richiesta di ") and card["rationale"] != notice.text

    await router.route_coordinator(choose(card["proposal_id"]))
    sent = await drain(queue)
    assert of_type(sent, CoordinatorNoticeMessage)[-1].text.startswith("Piano aggiornato:")
    assert (await by_id(store, proto)).progress == 50


async def test_a_new_owner_date_and_status_go_on_one_option(store):
    proto = await task(store, PROTOTYPE, owner=ADA)
    router, queue, _ = make(store, answer(
        "Card ready to confirm.",
        {"kind": "update_task", "task_id": proto, "owner_id": BRUNO, "due_date": "2026-11-28", "status": "doing"},
    ))
    _, (card,) = await ask(router, queue, "Cadence, Bruno takes over the prototype from now, due the 28th")
    assert card["rationale"].startswith("Asked by ")
    (option,) = card["payload"]["options"]
    assert option["label"] == f"“{PROTOTYPE}”: status: doing, owner Bruno, due 28 Nov"
    await router.route_coordinator(choose(card["proposal_id"]))
    await drain(queue)
    saved = await by_id(store, proto)
    assert (saved.owner_id, saved.due_date, saved.status) == (BRUNO, date(2026, 11, 28), TaskStatus.DOING)


async def test_an_update_the_plan_already_shows_gets_no_card(store):
    proto = await task(store, PROTOTYPE, owner=ADA)
    router, queue, _ = make(store, answer("Card pronta.", {"kind": "update_task", "task_id": proto, "owner_id": ADA}))
    notice, cards = await ask(router, queue, "Cadence, il prototipo è di Ada")
    assert cards == [] and "registra già" in notice.text


async def test_saying_it_again_points_to_the_card_above(store):
    proto = await task(store, PROTOTYPE, owner=ADA)
    action = {"kind": "update_task", "task_id": proto, "progress": 50}
    router, queue, _ = make(store, answer("Card pronta.", action), answer("Card pronta.", action))
    await ask(router, queue, "Cadence, segna il prototipo al 50%")
    notice, cards = await ask(router, queue, "Cadence, sì, il prototipo al 50%")
    assert cards == [] and "qui sopra" in notice.text


async def test_several_tasks_at_once_become_one_plan_card(store):
    await task(store, "Scrivere il RASD")
    items = [
        {"title": "Scrivere il RASD", "owner_id": None, "due_date": None},  # already in the plan: left out
        {"title": "Wireframe", "owner_id": ADA, "due_date": "2026-11-22"},
        {"title": "Test con utenti", "owner_id": BRUNO, "due_date": None},
    ]
    router, queue, _ = make(store, answer("Ecco le attività: conferma la card.", {"kind": "plan", "items": items}))
    notice, (card,) = await ask(router, queue, "Cadence, aggiungi RASD, wireframe per Ada entro il 22 e test con utenti per Bruno")
    assert card["kind"] == "plan"
    assert [i["title"] for i in card["payload"]["items"]] == ["Wireframe", "Test con utenti"]
    assert len((await store.load_state()).tasks) == 1

    await router.route_coordinator(click(card["proposal_id"], "confirm"))
    await drain(queue)
    saved = {t.title: t for t in (await store.load_state()).tasks}
    assert saved["Wireframe"].owner_id == ADA and saved["Wireframe"].due_date == date(2026, 11, 22)
    assert saved["Test con utenti"].owner_id == BRUNO


def test_the_action_check_refuses_updates_and_plans_that_make_no_sense():
    tasks = {"a": type("T", (), {"title": "A", "depends_on": []})()}
    members = {1: "Ada"}
    for bad in (
        {"kind": "update_task", "task_id": None, "progress": 50},  # which task?
        {"kind": "update_task", "task_id": "a"},  # changes nothing
        {"kind": "update_task", "task_id": "a", "progress": 150},
        {"kind": "update_task", "task_id": "a", "owner_id": 99},
        {"kind": "update_task", "task_id": "a", "due_date": "2026-01-01"},
        {"kind": "plan", "items": []},
        {"kind": "plan", "items": [{"title": "X", "owner_id": 99, "due_date": None}]},
    ):
        with pytest.raises(ValueError):
            ask_action(bad, tasks, members, date(2026, 11, 20))


async def test_the_sources_line_follows_the_answers_language(store):
    """A question with no Italian word ("50% prototipo?") still gets "Fonti" under an Italian answer."""
    proto = await task(store, PROTOTYPE, owner=ADA)
    router, queue, _ = make(store, answer("Il prototipo è di Ada.", task_ids=[proto]))
    notice, _ = await ask(router, queue, "Cadence, prototipo?")
    assert notice.text.endswith(f"Fonti: attività “{PROTOTYPE}”.")


async def test_a_split_replaces_the_task_it_splits(store):
    """
    22/11 live: "split the interviews in two, 5 for me and 5 for Omar" added
    two tasks and left "Run the 10 interviews" in the plan: three tasks.
    """
    whole = await task(store, "Run the 10 interviews", owner=ADA)
    items = [
        {"title": "Run 5 interviews (Ada)", "owner_id": ADA, "due_date": "2026-11-26"},
        {"title": "Run 5 interviews (Bruno)", "owner_id": BRUNO, "due_date": "2026-11-26"},
    ]
    router, queue, _ = make(
        store, answer("The split is ready on a card.", {"kind": "plan", "items": items, "replaces_task_id": whole})
    )
    notice, (card,) = await ask(router, queue, "Cadence, split the interviews in two: 5 for me and 5 for Bruno")
    assert card["payload"]["replaces_task_id"] == whole and card["payload"]["replaces_title"] == "Run the 10 interviews"
    assert len((await store.load_state()).tasks) == 1  # nothing changes before the click

    await router.route_coordinator(click(card["proposal_id"], "confirm"))
    await drain(queue)
    assert sorted(t.title for t in (await store.load_state()).tasks) == ["Run 5 interviews (Ada)", "Run 5 interviews (Bruno)"]


async def test_asked_to_drop_a_task_cadence_drafts_a_card_and_the_click_removes_it(store):
    """22/11 demo run: "drop the task Run the 10 interviews" got an answer that agreed, and no card."""
    whole = await task(store, "Run the 10 interviews", owner=ADA)
    router, queue, _ = make(store, answer("The removal is ready on a card.", {"kind": "update_task", "task_id": whole, "drop": True}))
    notice, (card,) = await ask(router, queue, "Cadence, drop the task Run the 10 interviews, we split it already")
    assert card["kind"] == "checkin" and card["payload"]["requested"] is True
    assert card["payload"]["options"][0]["label"] == "Remove “Run the 10 interviews” from the plan"
    assert len((await store.load_state()).tasks) == 1  # nothing changes before the click

    await router.route_coordinator(choose(card["proposal_id"]))
    await drain(queue)
    assert (await store.load_state()).tasks == []


async def test_what_a_member_says_about_themselves_is_saved_as_their_profile(store):
    """The answer to Cadence's welcome question: saved at once, no card (their own words about themselves)."""
    action = {"kind": "profile", "discipline": "Psychology", "skills": ["interviews", "surveys"], "weekly_hours": 6}
    router, queue, _ = make(store, answer("Noted. The interview guide could fit you.", action))
    notice, cards = await ask(router, queue, "psychology, interviews and surveys, 6 hours")
    assert cards == [] and notice.text.startswith("Noted.")
    (ada,) = [m for m in (await store.load_state()).members if m.member_id == ADA]
    assert (ada.discipline, ada.skills, ada.weekly_hours) == ("Psychology", ["interviews", "surveys"], 6)
