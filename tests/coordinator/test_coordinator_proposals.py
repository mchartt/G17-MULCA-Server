"""ProposalService: every transition and every check before applying."""

from __future__ import annotations

import pytest

from muca_server.cognition.coordination.models import (
    CharterPayload,
    ProposalKind,
    ProposalStatus,
)
from muca_server.cognition.coordination.proposals import ProposalService

from .conftest import GOOD_CHARTER

ROSTER = {1: "Ada", 2: "Bruno"}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def charter_payload() -> CharterPayload:
    data = dict(GOOD_CHARTER)
    data.pop("rationale")
    return CharterPayload.model_validate(data)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(store, clock):
    return ProposalService(store, ttl_seconds=60, clock=clock)


async def new_proposal(service):
    return await service.create(
        ProposalKind.CHARTER,
        charter_payload(),
        rationale="Stepping in because you pasted the assignment.",
        origin_channel="web",
    )


async def test_create_saves_a_pending_proposal_and_changes_no_state(service, store):
    proposal = await new_proposal(service)
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.expires_at == proposal.created_at + 60
    assert proposal.origin_channel == "web"
    assert (await store.get_proposal(proposal.proposal_id)) == proposal
    assert (await store.load_state()).charter is None


async def test_confirm_applies_the_charter_and_its_milestones(service, store):
    proposal = await new_proposal(service)
    outcome = await service.respond(
        proposal.proposal_id, action="confirm", member_id=2, participants=ROSTER
    )
    assert outcome.ok
    assert outcome.proposal.status is ProposalStatus.APPLIED
    assert outcome.proposal.resolved_by == 2
    state = await store.load_state()
    assert state.charter.goal == GOOD_CHARTER["goal"]
    assert state.charter.confirmed_by == 2
    assert [m.title for m in state.milestones] == ["Demo Madness", "Final tutoring"]
    assert state.milestones[1].due_date is None


async def test_reject_closes_it_and_changes_no_state(service, store):
    proposal = await new_proposal(service)
    outcome = await service.respond(
        proposal.proposal_id, action="reject", member_id=1, participants=ROSTER
    )
    assert outcome.ok
    assert outcome.proposal.status is ProposalStatus.REJECTED
    assert (await store.load_state()).charter is None


async def test_a_click_from_outside_the_group_is_refused(service, store):
    proposal = await new_proposal(service)
    outcome = await service.respond(
        proposal.proposal_id, action="confirm", member_id=99, participants=ROSTER
    )
    assert not outcome.ok
    assert "Only members" in outcome.message
    assert (await store.get_proposal(proposal.proposal_id)).status is ProposalStatus.PENDING


async def test_a_missing_proposal_is_refused(service):
    outcome = await service.respond("nope", action="confirm", member_id=1, participants=ROSTER)
    assert not outcome.ok
    assert outcome.proposal is None


async def test_the_second_click_is_refused(service, store):
    proposal = await new_proposal(service)
    await service.respond(proposal.proposal_id, action="confirm", member_id=1, participants=ROSTER)
    outcome = await service.respond(
        proposal.proposal_id, action="reject", member_id=2, participants=ROSTER
    )
    assert not outcome.ok
    assert "already applied" in outcome.message
    assert (await store.get_proposal(proposal.proposal_id)).status is ProposalStatus.APPLIED


async def test_an_expired_proposal_is_closed_as_expired_not_applied(service, store, clock):
    proposal = await new_proposal(service)
    clock.now += 61
    outcome = await service.respond(
        proposal.proposal_id, action="confirm", member_id=1, participants=ROSTER
    )
    assert not outcome.ok
    assert outcome.proposal.status is ProposalStatus.EXPIRED
    assert (await store.load_state()).charter is None


async def test_an_unknown_action_is_refused(service):
    proposal = await new_proposal(service)
    outcome = await service.respond(
        proposal.proposal_id, action="dance", member_id=1, participants=ROSTER
    )
    assert not outcome.ok
    assert outcome.proposal.status is ProposalStatus.PENDING


async def test_a_payload_that_became_invalid_is_not_applied(service, store):
    """Re-validation before applying: a corrupted payload never becomes state."""
    proposal = await new_proposal(service)
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE coord_proposal SET payload_json = '{\"goal\": \"\"}' WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        await db.commit()
    outcome = await service.respond(
        proposal.proposal_id, action="confirm", member_id=1, participants=ROSTER
    )
    assert not outcome.ok
    assert "no longer valid" in outcome.message
    assert (await store.get_proposal(proposal.proposal_id)).status is ProposalStatus.PENDING


async def test_two_confirms_at_once_apply_exactly_once(service, store):
    import asyncio

    proposal = await new_proposal(service)
    outcomes = await asyncio.gather(
        service.respond(proposal.proposal_id, action="confirm", member_id=1, participants=ROSTER),
        service.respond(proposal.proposal_id, action="confirm", member_id=2, participants=ROSTER),
    )
    assert sorted(o.ok for o in outcomes) == [False, True]
    assert len((await store.load_state()).milestones) == 2


async def test_an_old_charter_card_cannot_overwrite_a_newer_charter(service, store, clock):
    old = await new_proposal(service)
    clock.now += 10
    new = await new_proposal(service)
    clock.now += 10
    assert (await service.respond(new.proposal_id, action="confirm", member_id=1, participants=ROSTER)).ok
    clock.now += 10
    outcome = await service.respond(old.proposal_id, action="confirm", member_id=2, participants=ROSTER)
    assert not outcome.ok
    assert "newer charter" in outcome.message
    assert outcome.proposal.status is ProposalStatus.EXPIRED
    assert (await store.load_state()).charter.confirmed_by == 1
