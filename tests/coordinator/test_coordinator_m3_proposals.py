"""M3: the new card kinds and buttons (take, choose, not_now) in ProposalService."""

from __future__ import annotations

from datetime import date

import pytest

from muca_server.cognition.coordination.models import ProposalKind, ProposalStatus
from muca_server.cognition.coordination.plan_writes import add_task
from muca_server.cognition.coordination.proposals import SNOOZE_SECONDS, ProposalService

from .m3_fixtures import ROSTER, decision_payload, delegation_payload, task_payload
from .test_coordinator_proposals import Clock, charter_payload


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(store, clock):
    return ProposalService(store, ttl_seconds=600, clock=clock)


async def make(service, kind, payload):
    return await service.create(kind, payload, rationale="Stepping in because.", origin_channel="group")


async def test_confirm_on_cs5_gives_the_task_to_the_suggested_owner(service, store):
    card = await make(service, ProposalKind.TASK_OWNER, task_payload())
    out = await service.respond(card.proposal_id, action="confirm", member_id=1, participants=ROSTER)
    assert out.ok and out.proposal.status is ProposalStatus.APPLIED
    assert "Bruno owns" in out.message
    (task,) = (await store.load_state()).tasks
    assert (task.title, task.owner_id, task.due_date) == ("Write the evaluation protocol", 2, date(2026, 11, 27))


async def test_ill_take_it_makes_the_clicker_the_owner(service, store):
    card = await make(service, ProposalKind.TASK_OWNER, task_payload())
    out = await service.respond(card.proposal_id, action="take", member_id=3, participants=ROSTER)
    assert out.ok
    assert (await store.load_state()).tasks[0].owner_id == 3


async def test_cs5_without_a_suggestion_cannot_be_confirmed(service, store):
    card = await make(service, ProposalKind.TASK_OWNER, task_payload(owner_id=None))
    out = await service.respond(card.proposal_id, action="confirm", member_id=1, participants=ROSTER)
    assert not out.ok and "I'll take it" in out.message
    assert (await store.load_state()).tasks == []


async def test_a_suggested_owner_who_left_the_group_cannot_be_confirmed(service, store):
    card = await make(service, ProposalKind.TASK_OWNER, task_payload(owner_id=2))
    out = await service.respond(card.proposal_id, action="confirm", member_id=1, participants={1: "Ada"})
    assert not out.ok and "not in this group" in out.message
    assert (await store.get_proposal(card.proposal_id)).status is ProposalStatus.PENDING


async def test_delegation_is_only_taken_never_assigned(service, store):
    card = await make(service, ProposalKind.DELEGATION, delegation_payload())
    refused = await service.respond(card.proposal_id, action="confirm", member_id=1, participants=ROSTER)
    assert not refused.ok
    taken = await service.respond(card.proposal_id, action="take", member_id=1, participants=ROSTER)
    assert taken.ok
    (task,) = (await store.load_state()).tasks
    assert task.owner_id == 1 and task.due_date is None


async def test_cs5_on_an_existing_task_assigns_it(service, store):
    db = await store._connect()
    try:
        await add_task(db, task_id="t1", title="Slides", owner_id=None, due_date=None)
    finally:
        await db.close()
    card = await make(service, ProposalKind.TASK_OWNER, task_payload(task_id="t1", title="Slides"))
    assert (await service.respond(card.proposal_id, action="take", member_id=2, participants=ROSTER)).ok
    (task,) = (await store.load_state()).tasks
    assert (task.task_id, task.owner_id, task.due_date) == ("t1", 2, date(2026, 11, 27))


async def test_a_task_taken_meanwhile_closes_the_card_as_expired(service, store):
    db = await store._connect()
    try:
        await add_task(db, task_id="t1", title="Slides", owner_id=3, due_date=None)
    finally:
        await db.close()
    card = await make(service, ProposalKind.TASK_OWNER, task_payload(task_id="t1", title="Slides"))
    out = await service.respond(card.proposal_id, action="take", member_id=2, participants=ROSTER)
    assert not out.ok and "already has an owner" in out.message
    assert out.proposal.status is ProposalStatus.EXPIRED
    assert (await store.load_state()).tasks[0].owner_id == 3


async def test_a_task_removed_meanwhile_is_stale(service, store):
    card = await make(service, ProposalKind.TASK_OWNER, task_payload(task_id="gone"))
    out = await service.respond(card.proposal_id, action="take", member_id=2, participants=ROSTER)
    assert not out.ok and out.proposal.status is ProposalStatus.EXPIRED


async def test_choosing_an_option_records_the_decision_with_reason_and_source(service, store, clock):
    card = await make(service, ProposalKind.DECISION, decision_payload())
    out = await service.respond(card.proposal_id, action="choose", option=0, member_id=2, participants=ROSTER)
    assert out.ok and "Think-aloud" in out.message
    (decision,) = (await store.load_state()).decisions
    assert decision.choice == "Think-aloud"
    assert decision.reason == "Shows why users get stuck (Sofia)."
    assert decision.decided_by == 2 and decision.decided_at == clock.now
    assert decision.source == f"proposal:{card.proposal_id}"


@pytest.mark.parametrize("option", [None, -1, 2, 9])
async def test_a_missing_or_wrong_option_changes_nothing(service, store, option):
    card = await make(service, ProposalKind.DECISION, decision_payload())
    out = await service.respond(card.proposal_id, action="choose", option=option, member_id=2, participants=ROSTER)
    assert not out.ok
    assert (await store.load_state()).decisions == []
    assert (await store.get_proposal(card.proposal_id)).status is ProposalStatus.PENDING


async def test_not_now_snoozes_the_strategy_on_that_topic_for_30_minutes(service, store, clock):
    card = await make(service, ProposalKind.DELEGATION, delegation_payload())
    out = await service.respond(card.proposal_id, action="not_now", member_id=1, participants=ROSTER)
    assert out.ok and out.proposal.status is ProposalStatus.SNOOZED
    assert "30 minutes" in out.message
    assert await store.active_snoozes(clock.now) == {("cs2_delegate_simple_task", "fonts and margins")}
    assert await store.active_snoozes(clock.now + SNOOZE_SECONDS + 1) == set()
    assert (await store.load_state()).tasks == []


async def test_not_now_is_refused_on_a_card_the_group_asked_for(service, store, clock):
    card = await make(service, ProposalKind.CHARTER, charter_payload())
    out = await service.respond(card.proposal_id, action="not_now", member_id=1, participants=ROSTER)
    assert not out.ok
    assert await store.active_snoozes(clock.now) == set()


@pytest.mark.parametrize(
    ("kind", "payload", "action"),
    [
        (ProposalKind.CHARTER, "charter", "take"),
        (ProposalKind.CHARTER, "charter", "choose"),
        (ProposalKind.DECISION, "decision", "confirm"),
        (ProposalKind.DECISION, "decision", "take"),
        (ProposalKind.DELEGATION, "delegation", "choose"),
    ],
)
async def test_a_button_of_another_kind_of_card_is_refused(service, store, kind, payload, action):
    payloads = {"charter": charter_payload(), "decision": decision_payload(), "delegation": delegation_payload()}
    card = await make(service, kind, payloads[payload])
    out = await service.respond(card.proposal_id, action=action, option=0, member_id=1, participants=ROSTER)
    assert not out.ok and "does not apply" in out.message
    assert (await store.get_proposal(card.proposal_id)).status is ProposalStatus.PENDING


async def test_the_first_click_wins_between_take_and_not_now(service, store):
    card = await make(service, ProposalKind.DELEGATION, delegation_payload())
    assert (await service.respond(card.proposal_id, action="take", member_id=1, participants=ROSTER)).ok
    late = await service.respond(card.proposal_id, action="not_now", member_id=2, participants=ROSTER)
    assert not late.ok and "already applied" in late.message
