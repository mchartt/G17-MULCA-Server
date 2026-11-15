"""
M3: ProactiveBridge (rules R1-R7, card posted in the background), the
CoalaPolicy hook, the wiring, and the new clicks through the router.
"""

from __future__ import annotations

import asyncio
from datetime import date

from muca_server.cognition import InterventionSignal, Utterance
from muca_server.cognition.coordination.models import ProposalKind, ProposalStatus, Task
from muca_server.cognition.coordination.proactive import (
    MIN_MESSAGES_BETWEEN,
    MIN_SECONDS_BETWEEN,
    ProactiveBridge,
    Verdict,
)
from muca_server.cognition.coordination.proactive_writer import ProactiveDraft, ProactiveWriter
from muca_server.cognition.coordination.proposals import ProposalService
from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.wiring import attach_proactive
from muca_server.cognition.coordination.writer import ProposalWriter
from muca_server.cognition.strategies import Strategy, StrategyStore
from muca_server.protocol import CoordinatorNoticeMessage, CoordinatorProposalMessage, parse_incoming_message

from .conftest import FakeChatClient, FakeConversation, drain
from .m3_fixtures import decision_payload, delegation_payload, task_payload
from .test_coordinator_proposals import Clock

CS2 = "cs2_delegate_simple_task"
CS5 = "cs5_owner_and_date"


class FakeWriter:
    """Returns scripted drafts and records what the bridge passed in."""

    def __init__(self, *drafts) -> None:
        self.drafts = list(drafts)
        self.calls: list[dict] = []

    async def draft(self, strategy_id, **kwargs):
        self.calls.append(dict(kwargs, strategy_id=strategy_id))
        item = self.drafts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def utter(speaker_id, text, name="Ada", ts=0):
    return Utterance(speaker_id=speaker_id, speaker_name=name, content=text, timestamp_ms=ts)


HISTORY = [utter(1, "fonts?", "Ada"), utter(2, "Arial!", "Bruno"), utter(3, "no, Helvetica", "Chiara")]


def make_bridge(store, writer, conversation=None, clock=None):
    clock = clock or Clock()
    sent = []

    async def send(proposal):
        sent.append(proposal)

    bridge = ProactiveBridge(
        conversation or FakeConversation(),
        writer=writer,
        store=lambda: store,
        service=lambda: ProposalService(store, clock=clock),
        send_proposal=send,
        today=lambda: date(2026, 11, 20),
        clock=clock,
    )
    return bridge, sent, clock


async def offer(bridge, strategy=CS2, count=20, history=HISTORY):
    verdict = await bridge.offer(strategy, history=history, message_count=count, assistant_id=0)
    await bridge.wait()
    return verdict


def deleg_draft(topic="Fonts and margins"):
    payload = delegation_payload()
    payload = payload.model_copy(update={"context": payload.context.model_copy(update={"topic": topic})})
    return ProactiveDraft(ProposalKind.DELEGATION, payload, "Stepping in because one person can do it.")


async def test_a_lab_strategy_is_not_the_coordinators(store):
    bridge, sent, _ = make_bridge(store, FakeWriter())
    assert await bridge.offer("maintain_focus", history=HISTORY, message_count=5, assistant_id=0) is Verdict.NOT_MINE


async def test_a_fired_strategy_becomes_a_pending_card(store):
    writer = FakeWriter(deleg_draft())
    bridge, sent, _ = make_bridge(store, writer)
    assert await offer(bridge) is Verdict.TAKEN
    (card,) = sent
    assert card.kind is ProposalKind.DELEGATION and card.status is ProposalStatus.PENDING
    assert card.origin_channel == "group"
    # The writer saw the chat (without the agent), the roster and the plan.
    (call,) = writer.calls
    assert [line.text for line in call["chat"]] == ["fonts?", "Arial!", "no, Helvetica"]
    assert call["members"] == {1: "Ada", 2: "Bruno", 3: "Chiara"}


async def test_r1_never_in_a_dm(store):
    bridge, sent, _ = make_bridge(store, FakeWriter(), FakeConversation(is_private=True))
    assert await offer(bridge) is Verdict.VETOED and sent == []


async def test_r2_one_draft_at_a_time(store):
    gate = asyncio.Event()

    class Slow(FakeWriter):
        async def draft(self, strategy_id, **kwargs):
            await gate.wait()
            return deleg_draft()

    bridge, sent, _ = make_bridge(store, Slow())
    assert await bridge.offer(CS2, history=HISTORY, message_count=20, assistant_id=0) is Verdict.TAKEN
    assert await bridge.offer(CS5, history=HISTORY, message_count=21, assistant_id=0) is Verdict.VETOED
    gate.set()
    await bridge.wait()
    assert len(sent) == 1


async def test_r3_r4_ten_messages_and_five_minutes_between_cards(store):
    bridge, sent, clock = make_bridge(store, FakeWriter(deleg_draft("a"), deleg_draft("b")))
    assert await offer(bridge, count=20) is Verdict.TAKEN
    clock.now += MIN_SECONDS_BETWEEN + 1
    assert await offer(bridge, count=20 + MIN_MESSAGES_BETWEEN - 1) is Verdict.VETOED  # R3
    clock.now -= MIN_SECONDS_BETWEEN  # back inside the 5 minutes
    assert await offer(bridge, count=20 + MIN_MESSAGES_BETWEEN) is Verdict.VETOED  # R4
    clock.now += MIN_SECONDS_BETWEEN
    assert await offer(bridge, count=20 + MIN_MESSAGES_BETWEEN) is Verdict.TAKEN
    assert len(sent) == 2


async def test_r3_r4_survive_a_server_restart(store):
    """A new session (new bridge, same SQLite file) reads the last card's marker."""
    first, sent, clock = make_bridge(store, FakeWriter(deleg_draft("a")))
    assert await offer(first, count=20) is Verdict.TAKEN and len(sent) == 1
    assert await store.mark("proactive") == (20, clock.now)
    second, sent2, _ = make_bridge(store, FakeWriter(deleg_draft("b")), clock=clock)
    assert await offer(second, count=25) is Verdict.VETOED  # R3 still counts
    clock.now += MIN_SECONDS_BETWEEN + 1
    assert await offer(second, count=20 + MIN_MESSAGES_BETWEEN) is Verdict.TAKEN
    assert len(sent2) == 1


async def test_an_unreadable_marker_never_breaks_the_offer(store):
    class Broken:
        async def mark(self, name):
            raise OSError("disk")

    bridge, _, _ = make_bridge(store, FakeWriter(deleg_draft()))
    bridge._store = lambda: Broken()
    assert await bridge.offer(CS2, history=HISTORY, message_count=20, assistant_id=0) is Verdict.TAKEN
    await bridge.wait()  # the draft fails on the broken store, logged, chat goes on


async def test_a_failed_draft_does_not_silence_the_group(store):
    bridge, sent, _ = make_bridge(store, FakeWriter(None, deleg_draft()))
    assert await offer(bridge, count=20) is Verdict.TAKEN and sent == []
    assert await offer(bridge, count=21) is Verdict.TAKEN and len(sent) == 1


async def test_r5_never_right_after_the_agent(store):
    history = [utter(1, "hi"), utter(0, "agent reply", "MULCA"), utter(2, "ok")]
    bridge, sent, _ = make_bridge(store, FakeWriter())
    assert await offer(bridge, history=history) is Verdict.VETOED


async def test_r6_a_snoozed_topic_never_gets_a_card(store):
    bridge, sent, clock = make_bridge(store, FakeWriter(deleg_draft("FONTS and  margins.")))
    service = ProposalService(store, clock=clock)
    card = await service.create(ProposalKind.DELEGATION, delegation_payload(), rationale="x", origin_channel="web")
    await service.respond(card.proposal_id, action="not_now", member_id=1, participants={1: "Ada"})
    assert await offer(bridge) is Verdict.TAKEN
    assert sent == []  # dropped after the draft: same topic key (case, spaces, punctuation ignored)
    # ...and the writer was told, so it can avoid the topic in the first place.
    assert bridge._writer.calls[0]["silenced_topics"] == ["fonts and margins"]


async def test_r7_no_cs5_for_a_task_that_already_has_an_owner(store):
    db = await store._connect()
    try:
        await db.execute("INSERT INTO coord_task (task_id, title, owner_id, status) VALUES ('t', 'Write the evaluation protocol', 2, 'todo')")
    finally:
        await db.close()
    draft = ProactiveDraft(ProposalKind.TASK_OWNER, task_payload(), "Stepping in because.")
    bridge, sent, _ = make_bridge(store, FakeWriter(draft))
    assert await offer(bridge, CS5) is Verdict.TAKEN and sent == []


async def test_an_error_while_drafting_never_breaks_the_chat(store):
    bridge, sent, _ = make_bridge(store, FakeWriter(RuntimeError("db down")))
    assert await offer(bridge) is Verdict.TAKEN and sent == []


# ----------------------------------------------------------------------
# The CoalaPolicy hook (cognition/policy.py)
# ----------------------------------------------------------------------


def coordinator_policy():
    from tests.test_coala_policy import FakePort, make_policy

    strategies = StrategyStore(
        {
            CS2: Strategy(id=CS2, trigger_description="t", prompt_template="Reply. {prompt_fragment}", cooldown_messages=10),
            "maintain_focus": Strategy(id="maintain_focus", trigger_description="t", prompt_template="Focus. {prompt_fragment}"),
        }
    )
    return make_policy(strategy_store=strategies, port=FakePort())


class FakeBridge:
    def __init__(self, verdict):
        self.verdict = verdict
        self.offers = []

    async def offer(self, strategy_id, **kwargs):
        self.offers.append((strategy_id, kwargs))
        return self.verdict if strategy_id == CS2 else Verdict.NOT_MINE


async def judged(policy, port, strategy_id):
    await policy.start(port)
    await policy.on_utterance(utter(1, "fonts again"))
    await policy.on_intervention_signal(object(), InterventionSignal(strategy_id=strategy_id, prompt_fragment="x"))


async def test_a_taken_strategy_is_a_card_not_a_reply():
    policy, port = coordinator_policy()
    bridge = FakeBridge(Verdict.TAKEN)
    policy.set_proactive_bridge(bridge)
    await judged(policy, port, CS2)
    assert port.instructions == [] and port.no_reply_count == 1
    (strategy, kwargs), = bridge.offers
    assert strategy == CS2 and kwargs["message_count"] == 1 and kwargs["history"][-1].content == "fonts again"
    # The strategy's own cooldown starts, as for any strategy that fired.
    assert policy._wm.recent_actions[CS2] == 1


async def test_a_vetoed_strategy_is_silence_without_cooldown():
    policy, port = coordinator_policy()
    policy.set_proactive_bridge(FakeBridge(Verdict.VETOED))
    await judged(policy, port, CS2)
    assert port.instructions == [] and port.no_reply_count == 1
    assert CS2 not in policy._wm.recent_actions


async def test_other_strategies_keep_the_lab_path():
    policy, port = coordinator_policy()
    policy.set_proactive_bridge(FakeBridge(Verdict.TAKEN))
    await judged(policy, port, "maintain_focus")
    assert len(port.instructions) == 1 and "Focus." in port.instructions[0]


async def test_without_a_bridge_the_policy_is_unchanged():
    policy, port = coordinator_policy()
    await judged(policy, port, CS2)
    assert len(port.instructions) == 1 and "Reply." in port.instructions[0]


# ----------------------------------------------------------------------
# Wiring and router
# ----------------------------------------------------------------------


def make_router(store, conversation=None, proactive=True):
    queue: asyncio.Queue = asyncio.Queue()
    base = ProposalWriter(FakeChatClient())
    router = CoordinatorRouter(
        conversation or FakeConversation(),
        queue,
        store_for=lambda _: store,
        writer=base,
        proposal_ttl_seconds=3600,
        proactive_writer=ProactiveWriter(base) if proactive else None,
    )
    return router, queue


async def test_attach_plugs_the_bridge_into_the_policy(store):
    class Policy:
        bridge = None

        def set_proactive_bridge(self, bridge):
            self.bridge = bridge

    conversation = FakeConversation()
    conversation._policy = Policy()
    router, _ = make_router(store, conversation)
    assert attach_proactive(conversation, router) is True
    assert conversation._policy.bridge is router.proactive


async def test_attach_is_harmless_without_the_hook(store):
    conversation = FakeConversation()
    conversation._policy = object()  # e.g. NoCognition
    router, _ = make_router(store, conversation)
    assert attach_proactive(conversation, router) is False
    router, _ = make_router(store, FakeConversation(), proactive=False)
    assert router.proactive is None and attach_proactive(FakeConversation(), router) is False


async def test_take_choose_and_not_now_go_through_the_router(store):
    router, queue = make_router(store)
    service = ProposalService(store, ttl_seconds=3600)
    decision = await service.create(ProposalKind.DECISION, decision_payload(), rationale="x", origin_channel="group")
    delegation = await service.create(ProposalKind.DELEGATION, delegation_payload(), rationale="x", origin_channel="group")

    def click(pid, action, member=2, **extra):
        return parse_incoming_message(
            {"event": "proposal_response", "proposalId": pid, "action": action,
             "memberId": member, "channel": "web", "requestId": "c", **extra}
        )

    await router.route_coordinator(click(decision.proposal_id, "choose", option=1))
    await router.route_coordinator(click(delegation.proposal_id, "not_now"))
    out = await drain(queue)
    cards = [m.proposal for m in out if isinstance(m, CoordinatorProposalMessage)]
    assert [c["status"] for c in cards] == ["applied", "snoozed"]
    notices = [m for m in out if isinstance(m, CoordinatorNoticeMessage)]
    assert notices[0].ok and "SUS questionnaire" in notices[0].text
    assert notices[1].ok and "30 minutes" in notices[1].text
    state = await store.load_state()
    assert state.decisions[0].choice == "SUS questionnaire"
    assert state.tasks == [] or all(isinstance(t, Task) for t in state.tasks)
