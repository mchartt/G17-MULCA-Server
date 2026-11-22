"""
Two choices a member makes by hand, with no model call: the language of the
coordinator's texts ("language"), and an edit of the charter ("charter"),
which is saved only after a member confirms its card.
"""

from __future__ import annotations

import asyncio
from datetime import date

from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.writer import ProposalWriter, language_rule
from muca_server.protocol import (
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
)

from .conftest import GOOD_CHARTER, FakeChatClient, FakeConversation, drain
from .test_coordinator_router import click, command, of_type


def make(store, *answers):
    client = FakeChatClient(*answers)
    queue: asyncio.Queue = asyncio.Queue()
    router = CoordinatorRouter(
        FakeConversation(),
        queue,
        store_for=lambda conversation_id: store,
        writer=ProposalWriter(client),
        proposal_ttl_seconds=3600,
        today=lambda: date(2026, 11, 15),
    )
    return router, queue, client


def system_of(call) -> str:
    return call["messages"][0]["content"]


async def test_a_group_that_chose_no_language_gets_the_prompt_as_before(store):
    router, queue, client = make(store, GOOD_CHARTER)
    await router.route_coordinator(command("kickoff", text="Assignment"))
    await router.wait_for_background()
    assert "LANGUAGE:" not in system_of(client.calls[0])
    assert (await store.load_state()).language == ""


async def test_the_chosen_language_is_saved_sent_with_the_state_and_added_to_every_prompt(store):
    router, queue, client = make(store, GOOD_CHARTER)
    await router.route_coordinator(command("language", language="it"))
    sent = await drain(queue)
    (notice,) = of_type(sent, CoordinatorNoticeMessage)
    assert notice.ok and "Italian" in notice.text
    assert of_type(sent, CoordinatorStateMessage)[0].state["language"] == "it"

    await router.route_coordinator(command("kickoff", text="An assignment written in English"))
    await router.wait_for_background()
    system = system_of(client.calls[0])
    assert system.endswith(language_rule("it"))
    assert "Write every text you produce in Italian" in system and "Intervengo perché" in system


async def test_the_language_is_the_groups_not_the_servers(store, tmp_path):
    """Two groups share one writer (wiring.py): one group's choice must not reach the other."""
    from muca_server.cognition.coordination.store import CoordinationStore

    client = FakeChatClient(GOOD_CHARTER, GOOD_CHARTER)
    shared = ProposalWriter(client)
    other_store = CoordinationStore(tmp_path / "other.db")
    routers = [
        CoordinatorRouter(FakeConversation(conversation_id=cid), asyncio.Queue(), store_for=lambda _, s=s: s,
                          writer=shared, proposal_ttl_seconds=3600, today=lambda: date(2026, 11, 15))
        for cid, s in (("web:G1", store), ("web:G2", other_store))
    ]
    await routers[0].route_coordinator(command("language", language="en"))
    for router in routers:
        await router.route_coordinator(command("kickoff", text="x"))
        await router.wait_for_background()
    assert "this group chose English" in system_of(client.calls[0])
    assert "LANGUAGE:" not in system_of(client.calls[1])


async def test_auto_goes_back_to_following_the_chat_and_an_unknown_language_is_refused(store):
    router, queue, _ = make(store)
    await router.route_coordinator(command("language", language="it"))
    await router.route_coordinator(command("language", language="klingon"))
    refused = of_type(await drain(queue), CoordinatorNoticeMessage)[-1]
    assert not refused.ok
    assert (await store.load_state()).language == "it"
    await router.route_coordinator(command("language", language="auto"))
    assert (await store.load_state()).language == ""


async def test_a_non_member_cannot_choose_the_language_or_edit_the_charter(store):
    router, queue, _ = make(store)
    await router.route_coordinator(command("language", member=77, language="it"))
    await router.route_coordinator(command("charter", member=77, goal="Mine now"))
    assert all(not n.ok for n in of_type(await drain(queue), CoordinatorNoticeMessage))
    state = await store.load_state()
    assert state.language == "" and state.charter is None


async def confirmed_charter(store):
    router, queue, client = make(store, GOOD_CHARTER)
    await router.route_coordinator(command("kickoff", text="x"))
    await router.wait_for_background()
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)
    await router.route_coordinator(click(card.proposal["proposal_id"]))
    await drain(queue)
    return router, queue, client


async def test_an_edited_charter_is_a_card_and_changes_nothing_until_a_member_confirms(store):
    router, queue, client = await confirmed_charter(store)
    calls_before = len(client.calls)

    await router.route_coordinator(
        command("charter", member=2, goal="  Build Cadence,\n the group coordinator. ", deliverables=["Report", " ", "Demo"])
    )
    sent = await drain(queue)
    (card,) = of_type(sent, CoordinatorProposalMessage)
    assert card.proposal["kind"] == "charter" and card.proposal["status"] == "pending"
    assert card.proposal["payload"]["goal"] == "Build Cadence, the group coordinator."
    assert card.proposal["payload"]["deliverables"] == ["Report", "Demo"]
    assert card.proposal["rationale"] == "Bruno edited the goal and deliverables."
    assert len(client.calls) == calls_before  # written by the member: no model call
    assert (await store.load_state()).charter.goal == GOOD_CHARTER["goal"]

    await router.route_coordinator(click(card.proposal["proposal_id"], member=3))
    state = await store.load_state()
    assert state.charter.goal == "Build Cadence, the group coordinator."
    assert state.charter.deliverables == ["Report", "Demo"]
    assert state.charter.confirmed_by == 3
    # The milestones are not part of the edit: they stay as they were.
    assert [(m.title, m.due_date) for m in state.milestones] == [("Demo Madness", date(2026, 12, 2)), ("Final tutoring", None)]


async def test_a_rejected_edit_leaves_the_charter_as_it_was(store):
    router, queue, _ = await confirmed_charter(store)
    await router.route_coordinator(command("charter", goal="Something else"))
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)
    await router.route_coordinator(click(card.proposal["proposal_id"], action="reject", member=2))
    assert (await store.load_state()).charter.goal == GOOD_CHARTER["goal"]


async def test_an_empty_or_unchanged_edit_makes_no_card(store):
    router, queue, _ = await confirmed_charter(store)
    await router.route_coordinator(command("charter", goal="   "))
    await router.route_coordinator(command("charter", goal=GOOD_CHARTER["goal"], deliverables=GOOD_CHARTER["deliverables"]))
    sent = await drain(queue)
    assert not of_type(sent, CoordinatorProposalMessage)
    assert [n.ok for n in of_type(sent, CoordinatorNoticeMessage)] == [False, False]


async def test_deliverables_typed_on_one_line_are_split(store):
    router, queue, _ = await confirmed_charter(store)
    await router.route_coordinator(command("charter", goal="New goal", deliverables="Report; Demo\nVideo"))
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)
    assert card.proposal["payload"]["deliverables"] == ["Report", "Demo", "Video"]
