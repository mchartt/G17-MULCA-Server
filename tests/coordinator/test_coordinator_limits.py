"""
Known limits closed after M5: "Helpful" (FR21), the card sent back on
request ("Why?" after a client restart), the timed check-in (FR15) and the
router as the session's worker. No network, model faked.
"""

from __future__ import annotations

import asyncio
from datetime import date

from muca_server.cognition.coordination.checkin_timer import MARK, CheckinTimer
from muca_server.cognition.coordination.models import ProposalKind
from muca_server.cognition.coordination.proposals import ProposalService
from muca_server.cognition.coordination.settings import CoordinatorSettings
from muca_server.cognition.coordination.wiring import build_coordinator_factory
from muca_server.protocol import CoordinatorNoticeMessage, CoordinatorProposalMessage
from muca_server.protocol.outgoing import serialize_outgoing_message

from .conftest import FakeChatClient, FakeConversation, drain
from .m3_fixtures import delegation_payload
from .test_coordinator_m4 import click, command, make_router, run, seed
from .test_coordinator_proposals import Clock, charter_payload

LATE = dict(task_id="a", title="Server", owner_id=1, due_date=date(2026, 11, 24))
OPTIONS = {
    "options": [
        {"label": "Move Server to 28 Nov", "task_id": "a", "new_due_date": "2026-11-28", "new_owner_id": None, "drop": False}
    ],
    "rationale": "Stepping in because Server is late.",
}


async def proactive_card(store):
    service = ProposalService(store, ttl_seconds=3600)
    return await service.create(ProposalKind.DELEGATION, delegation_payload(), rationale="x", origin_channel="group")


# ----------------------------------------------------------------------
# Helpful
# ----------------------------------------------------------------------


async def test_helpful_is_counted_once_per_member_and_changes_nothing(store):
    card = await proactive_card(store)
    router, queue = make_router(store)
    await run(router, click(card.proposal_id, "helpful", member=1))
    await run(router, click(card.proposal_id, "helpful", member=1))
    await run(router, click(card.proposal_id, "helpful", member=2))
    out = await drain(queue)
    assert not any(isinstance(m, CoordinatorProposalMessage) for m in out)  # card not redrawn
    texts = [m.text for m in out]
    assert texts == [
        "Thanks: noted as helpful (1 member so far).",
        "Thanks: noted as helpful (1 member so far).",
        "Thanks: noted as helpful (2 members so far).",
    ]
    assert (await store.get_proposal(card.proposal_id)).status == "pending"


async def test_helpful_only_on_proactive_cards_and_only_from_members(store):
    service = ProposalService(store, ttl_seconds=3600)
    charter = await service.create(ProposalKind.CHARTER, charter_payload(), rationale="x", origin_channel="web")
    router, queue = make_router(store)
    await run(router, click(charter.proposal_id, "helpful"))
    await run(router, click("nope", "helpful"))
    await run(router, click(charter.proposal_id, "helpful", member=99))
    notices = await drain(queue)
    assert [n.ok for n in notices] == [False, False, False]
    assert "posted on its own" in notices[0].text and "not in this group" in notices[1].text
    assert "Only members" in notices[2].text


# ----------------------------------------------------------------------
# Card on request
# ----------------------------------------------------------------------


async def test_card_command_sends_the_card_back_only_in_the_notice(store):
    card = await proactive_card(store)
    router, queue = make_router(store)
    await run(router, command("card", proposal_id=card.proposal_id))
    await run(router, command("card", proposal_id="missing"))
    found, missing = await drain(queue)
    assert found.ok and found.proposal["proposal_id"] == card.proposal_id
    assert found.proposal["payload"]["context"]["evidence"]
    assert not missing.ok and missing.proposal is None
    # Without a card the notice keeps its old shape on the wire.
    assert "proposal" not in serialize_outgoing_message(missing)
    assert '"proposal"' in serialize_outgoing_message(found)


# ----------------------------------------------------------------------
# Timed check-in
# ----------------------------------------------------------------------


def timer_for(store, conversation=None, interval=3600, ran=None, clock=None):
    ran = ran if ran is not None else []

    async def run_checkin():
        ran.append(True)
        return True

    timer = CheckinTimer(
        conversation or FakeConversation(), interval_seconds=interval, store=lambda: store,
        run_checkin=run_checkin, clock=clock or Clock(),
    )
    return timer, ran


async def test_timer_starts_the_clock_then_runs_once_per_interval(store):
    clock = Clock()
    timer, ran = timer_for(store, clock=clock)
    assert await timer.tick() is False and ran == []  # first look: the interval starts now
    clock.now += 1800
    assert await timer.tick() is False
    clock.now += 1801
    assert await timer.tick() is True and len(ran) == 1
    assert await timer.tick() is False  # not again until the next interval
    assert (await store.mark(MARK))[1] == clock.now


async def test_timer_mark_survives_a_restart(store):
    clock = Clock()
    first, _ = timer_for(store, clock=clock)
    await first.tick()
    clock.now += 3601
    second, ran = timer_for(store, clock=clock)  # new session, same file
    assert await second.tick() is True and ran == [True]


async def test_timer_never_runs_in_a_dm_or_without_conversation_id(store):
    for conversation in (FakeConversation(is_private=True), FakeConversation(is_ephemeral=True)):
        timer, ran = timer_for(store, conversation=conversation)
        assert await timer.tick() is False
    assert await store.mark(MARK) is None


async def test_timer_off_never_starts_and_close_is_safe(store):
    timer, _ = timer_for(store, interval=0)
    timer.start()
    assert timer._task is None
    await timer.close()
    on, _ = timer_for(store)
    on.start()
    assert on._task is not None
    await on.close()
    assert on._task is None


async def test_timed_checkin_posts_a_card_only_when_something_is_at_risk(store):
    router, queue = make_router(store)  # healthy plan: no model call
    await seed(store, dict(task_id="b", title="Slides", owner_id=2, due_date=date(2026, 12, 20)))
    assert await router._plan.timed_checkin() is False
    assert await drain(queue) == []  # silent: nobody asked
    await seed(store, LATE)
    router, queue = make_router(store, OPTIONS)
    assert await router._plan.timed_checkin() is True
    (card,) = await drain(queue)
    assert isinstance(card, CoordinatorProposalMessage) and card.proposal["kind"] == "checkin"
    assert card.proposal["origin_channel"] == "group"


async def test_timed_checkin_is_silent_when_the_model_fails(store):
    await seed(store, LATE)
    router, queue = make_router(store, "not json", "still not json")
    assert await router._plan.timed_checkin() is False
    assert await drain(queue) == []


async def test_timed_checkin_waits_for_a_running_member_command(store):
    router, _ = make_router(store)
    gate = asyncio.Event()
    router._plan._task = asyncio.create_task(gate.wait())
    assert await router._plan.timed_checkin() is False
    gate.set()
    await router._plan.wait()


# ----------------------------------------------------------------------
# Settings, wiring, session worker
# ----------------------------------------------------------------------


def test_checkin_interval_from_the_environment():
    on = {"MUCA_COORDINATOR_ENABLED": "true"}
    assert CoordinatorSettings.from_env(on).checkin_interval_seconds == 0
    assert CoordinatorSettings.from_env({**on, "MUCA_COORDINATOR_CHECKIN_HOURS": "24"}).checkin_interval_seconds == 86400
    assert CoordinatorSettings.from_env({**on, "MUCA_COORDINATOR_CHECKIN_HOURS": "-3"}).checkin_interval_seconds == 0


async def test_router_starts_and_closes_the_conversation_and_its_timer(tmp_path):
    class Conversation(FakeConversation):
        events: list

        async def start(self):
            self.events.append("start")

        async def close(self):
            self.events.append("close")

    conversation = Conversation()
    conversation.events = []
    settings = CoordinatorSettings(enabled=True, checkin_interval_seconds=3600)
    factory = build_coordinator_factory(settings, client=FakeChatClient(), db_dir=str(tmp_path))
    router = factory(conversation, asyncio.Queue())
    await router.start()
    assert router.checkin_timer._task is not None
    await router.close()
    assert conversation.events == ["start", "close"] and router.checkin_timer._task is None


# ----------------------------------------------------------------------
# Deleting a group's data on MUCA-Server
# ----------------------------------------------------------------------


def test_delete_conversation_removes_the_file_only_with_the_admin_token(tmp_path):
    from fastapi.testclient import TestClient

    from muca_server import create_app
    from muca_server.cognition.memory.sqlite import db_path_for

    path = db_path_for("web:ABCDEFGHJK", base_dir=tmp_path)
    path.write_bytes(b"x")
    client = TestClient(create_app(db_dir=tmp_path, admin_token="s3cret-token"))
    url = "/conversation?conversation_id=web:ABCDEFGHJK"
    assert client.delete(url).status_code == 403
    assert client.delete(url, headers={"x-admin-token": "wrong"}).status_code == 403
    assert path.is_file()
    ok = client.delete(url, headers={"x-admin-token": "s3cret-token"})
    assert ok.status_code == 200 and ok.json() == {"deleted": True} and not path.exists()
    assert client.delete(url, headers={"x-admin-token": "s3cret-token"}).json() == {"deleted": False}
    # No token configured: the endpoint is off.
    off = TestClient(create_app(db_dir=tmp_path))
    assert off.delete(url, headers={"x-admin-token": ""}).status_code == 403


async def test_timer_loop_waits_first_then_keeps_looking_even_after_an_error(store):
    """The loop: one wait for the members, then tick, wait, tick... an error never stops it."""
    from muca_server.cognition.coordination.checkin_timer import FIRST_LOOK_SECONDS, POLL_SECONDS

    waits, ticks = [], []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    timer = CheckinTimer(FakeConversation(), interval_seconds=3600, store=lambda: store, run_checkin=None, sleep=sleep)

    async def tick():
        ticks.append(True)
        raise RuntimeError("model down")

    timer.tick = tick
    try:
        await timer._loop()
    except asyncio.CancelledError:
        pass
    assert waits == [FIRST_LOOK_SECONDS, POLL_SECONDS, POLL_SECONDS] and len(ticks) == 2


def test_coordinator_model_from_the_environment_default_unchanged(tmp_path):
    from muca_server.cognition.coordination.writer import DEFAULT_WRITER_MODEL

    on = {"MUCA_COORDINATOR_ENABLED": "true"}
    assert CoordinatorSettings.from_env(on).writer_model == DEFAULT_WRITER_MODEL == "gpt-4o"
    settings = CoordinatorSettings.from_env({**on, "MUCA_COORDINATOR_MODEL": "gpt-6.1-sol"})
    assert settings.writer_model == "gpt-6.1-sol"
    router = build_coordinator_factory(settings, client=FakeChatClient(), db_dir=str(tmp_path))(FakeConversation(), asyncio.Queue())
    assert router._writer._model == "gpt-6.1-sol"


def test_the_configured_text_models_are_priced():
    """An unpriced model would count as free and slip past the spending limits."""
    from muca_server.agents.openai.main_agent import PRICES_PER_MILLION

    for model in ("gpt-6.1-sol", "gpt-6-luna", "gpt-4o", "gpt-5", "gpt-realtime-2.1-mini", "text-embedding-3-small"):
        assert model in PRICES_PER_MILLION
