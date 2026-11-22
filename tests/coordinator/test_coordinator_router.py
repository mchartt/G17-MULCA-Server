"""CoordinatorRouter: commands, card clicks, guards and the error path."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from muca_server.cognition.coordination.router import (
    GROUP_ONLY,
    UNAVAILABLE,
    CoordinatorRouter,
)
from muca_server.cognition.coordination.writer import ProposalWriter
from muca_server.protocol import (
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
    parse_incoming_message,
)

from .conftest import GOOD_CHARTER, FakeChatClient, FakeConversation, drain


def make_router(store, *answers, conversation=None):
    queue: asyncio.Queue = asyncio.Queue()
    router = CoordinatorRouter(
        conversation or FakeConversation(),
        queue,
        store_for=lambda conversation_id: store,
        writer=ProposalWriter(FakeChatClient(*answers)),
        proposal_ttl_seconds=3600,
        today=lambda: date(2026, 11, 15),
    )
    return router, queue


def command(name, member=1, **args):
    return parse_incoming_message(
        {"event": "coordinator_command", "command": name, "memberId": member,
         "args": args, "requestId": "r1"}
    )


def click(proposal_id, action="confirm", member=1, channel="web"):
    return parse_incoming_message(
        {"event": "proposal_response", "proposalId": proposal_id,
         "action": action, "memberId": member, "channel": channel, "requestId": "c1"}
    )


def of_type(messages, cls):
    return [m for m in messages if isinstance(m, cls)]


async def test_it_forwards_everything_else_to_the_conversation(store):
    router, _ = make_router(store)
    assert router.listening_mode == "ambient"
    assert router.participants() == {1: "Ada", 2: "Bruno", 3: "Chiara"}


async def test_kickoff_then_confirm_saves_the_charter(store):
    router, queue = make_router(store, GOOD_CHARTER)

    await router.route_coordinator(command("kickoff", text="Assignment text"))
    await router.wait_for_background()
    sent = await drain(queue)
    (card,) = of_type(sent, CoordinatorProposalMessage)
    (posted,) = of_type(sent, CoordinatorNoticeMessage)
    assert posted.ok and posted.request_id == "r1"  # the command gets its answer
    assert card.proposal["status"] == "pending"
    assert card.proposal["kind"] == "charter"
    assert card.proposal["origin_channel"] == "discord"
    assert (await store.load_state()).charter is None  # nothing before the click

    await router.route_coordinator(click(card.proposal["proposal_id"], member=3))
    sent = await drain(queue)
    assert of_type(sent, CoordinatorProposalMessage)[0].proposal["status"] == "applied"
    assert of_type(sent, CoordinatorNoticeMessage)[0].ok
    state = of_type(sent, CoordinatorStateMessage)[0].state
    assert state["charter"]["confirmed_by"] == 3
    assert [m["title"] for m in state["milestones"]] == ["Demo Madness", "Final tutoring"]


async def test_a_click_from_a_non_member_is_refused(store):
    router, queue = make_router(store, GOOD_CHARTER)
    await router.route_coordinator(command("kickoff", text="x"))
    await router.wait_for_background()
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)

    await router.route_coordinator(click(card.proposal["proposal_id"], member=77))
    sent = await drain(queue)
    (notice,) = of_type(sent, CoordinatorNoticeMessage)
    assert not notice.ok
    assert notice.request_id == "c1"  # answers the right click
    assert not of_type(sent, CoordinatorStateMessage)
    assert (await store.load_state()).charter is None


async def test_kickoff_without_text_asks_for_it(store):
    router, queue = make_router(store)
    await router.route_coordinator(command("kickoff"))
    (notice,) = await drain(queue)
    assert not notice.ok and notice.request_id == "r1"


async def test_kickoff_when_the_model_fails_says_unavailable(store):
    router, queue = make_router(store, RuntimeError("down"), RuntimeError("down"))
    await router.route_coordinator(command("kickoff", text="x"))
    await router.wait_for_background()
    (notice,) = await drain(queue)
    assert (notice.ok, notice.text) == (False, UNAVAILABLE)


async def test_profile_is_saved_with_skills_from_a_comma_list(store):
    router, queue = make_router(store)
    await router.route_coordinator(
        command("profile", member=2, discipline="Design", skills="figma, ux ,", weekly_hours=5)
    )
    sent = await drain(queue)
    assert of_type(sent, CoordinatorNoticeMessage)[0].ok
    (member,) = of_type(sent, CoordinatorStateMessage)[0].state["members"]
    assert member == {
        "member_id": 2, "display_name": "Bruno", "discipline": "Design",
        "skills": ["figma", "ux"], "weekly_hours": 5, "background": "",
    }


async def test_a_bad_profile_is_explained_not_saved(store):
    router, queue = make_router(store)
    await router.route_coordinator(command("profile", weekly_hours=500))
    (notice,) = await drain(queue)
    assert not notice.ok and "weekly_hours" in notice.text
    assert (await store.load_state()).members == []


async def test_a_non_member_cannot_have_a_profile(store):
    router, queue = make_router(store)
    await router.route_coordinator(command("profile", member=99))
    (notice,) = await drain(queue)
    assert not notice.ok


async def test_state_command_answers_with_the_request_id(store):
    router, queue = make_router(store)
    await router.route_coordinator(command("state"))
    (state,) = await drain(queue)
    assert isinstance(state, CoordinatorStateMessage) and state.request_id == "r1"


@pytest.mark.parametrize("field", ["is_private", "is_ephemeral"])
async def test_dms_and_ephemeral_sessions_are_refused(store, field):
    conversation = FakeConversation(**{field: True})
    router, queue = make_router(store, conversation=conversation)
    await router.route_coordinator(command("state"))
    (notice,) = await drain(queue)
    assert (notice.ok, notice.text) == (False, GROUP_ONLY)


async def test_an_unexpected_error_never_breaks_the_chat(store, monkeypatch):
    router, queue = make_router(store)

    async def explode():
        raise OSError("database locked")

    monkeypatch.setattr(store, "load_state", explode)
    await router.route_coordinator(command("state"))
    (notice,) = await drain(queue)
    assert (notice.ok, notice.text) == (False, UNAVAILABLE)


async def test_kickoff_runs_in_the_background_and_only_once_at_a_time(store):
    """The session must keep reading messages while the model writes."""
    release = asyncio.Event()

    class SlowClient(FakeChatClient):
        async def _create(self, **kwargs):
            await release.wait()
            return await super()._create(**kwargs)

    queue: asyncio.Queue = asyncio.Queue()
    router = CoordinatorRouter(
        FakeConversation(), queue,
        store_for=lambda conversation_id: store,
        writer=ProposalWriter(SlowClient(GOOD_CHARTER)),
        proposal_ttl_seconds=3600,
    )
    await asyncio.wait_for(router.route_coordinator(command("kickoff", text="x")), 1)
    await router.route_coordinator(command("kickoff", text="again"))
    (busy,) = await drain(queue)
    assert not busy.ok and "already running" in busy.text
    await router.route_coordinator(command("state"))  # not blocked meanwhile
    assert isinstance((await drain(queue))[0], CoordinatorStateMessage)
    release.set()
    await router.wait_for_background()
    assert of_type(await drain(queue), CoordinatorProposalMessage)


@pytest.mark.parametrize("name", ["kickoff", "state", "profile"])
async def test_a_non_member_cannot_run_any_command(store, name):
    client_answers = []  # an empty fake model: any call would fail the test
    router, queue = make_router(store, *client_answers)
    await router.route_coordinator(command(name, member=99, text="x"))
    await router.wait_for_background()
    (notice,) = await drain(queue)
    assert not notice.ok and "Only members" in notice.text


async def test_a_kickoff_in_a_channel_with_no_project_sends_the_group_to_the_web(store):
    """
    22/11: a project is created on the web (name, description, documents).
    A kickoff in a Discord or Slack channel that is not linked made a project
    no web space showed; now it says where a project starts and how to link.
    """
    from muca_server.cognition.coordination.router import kickoff_on_web

    for cid in ("channel:7", "slack:C7"):
        client = FakeChatClient(GOOD_CHARTER)
        out: asyncio.Queue = asyncio.Queue()
        router = CoordinatorRouter(FakeConversation(conversation_id=cid), out, store_for=lambda _: store,
                                   writer=ProposalWriter(client), proposal_ttl_seconds=3600)
        await router.route_coordinator(command("kickoff", text="Deliver by 2 Dec."))
        await router.wait_for_background()
        notice = out.get_nowait()
        assert not notice.ok and notice.text == kickoff_on_web()
        assert "/coordinator link" in notice.text and "/coordinator me" in notice.text
        assert client.calls == [] and (await store.load_state()).charter is None  # no model call, nothing saved


def test_the_kickoff_notice_names_the_web_app_when_the_server_knows_its_address(monkeypatch):
    from muca_server.cognition.coordination.router import kickoff_on_web

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://cadence.example/")
    assert "in the web app (https://cadence.example)" in kickoff_on_web()
