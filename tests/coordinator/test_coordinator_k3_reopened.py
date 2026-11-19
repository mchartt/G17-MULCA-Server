"""
K3: a decision in the log is being reopened (scenario C, tutor 18/11).

The card recalls the choice and its reason (copied from the log, never from
the model), says what is new, and offers Keep / Reopen it / Not now.
No network, model faked.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from muca_server.cognition.coordination.models import (
    REVISIT_KEEP,
    REVISIT_REOPEN,
    Decision,
    ProjectState,
    ProposalKind,
    ProposalStatus,
    RevisitPayload,
)
from muca_server.cognition.coordination.plan_writes import add_decision
from muca_server.cognition.coordination.proactive import _dropped
from muca_server.cognition.coordination.proactive_writer import ChatLine, ProactiveDraft, ProactiveWriter
from muca_server.cognition.coordination.project_memory import project_memory
from muca_server.cognition.coordination.proposals import ProposalService
from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.cognition.coordination.writer import ProposalWriter

from .conftest import FakeChatClient
from .m3_fixtures import ROSTER, context

CS = "cs3_reopened_decision"
TODAY = date(2026, 11, 19)
TABS = Decision(
    decision_id="d1", question="App navigation", choice="Tabs",
    reason="Five sections fit and testers missed the side menu", decided_by=3, decided_at=100.0, source="proposal:p1",
)
CHAT_C = [
    ChatLine("Ada", "what if we use a side menu instead of tabs? looks cleaner", 1),
    ChatLine("Bruno", "hmm why did we pick tabs again?", 2),
    ChatLine("Chiara", "no idea honestly", 3),
]
ANSWER = {
    "decision_id": "d1",
    "new_point": "Ada finds the side menu cleaner",
    "topic": "Navigation reopened",
    "evidence": [1, 2],
    "rationale": "Stepping in because tabs were chosen on purpose and the reason seems forgotten.",
}


async def draft(*answers, decisions=(TABS,)):
    client = FakeChatClient(*answers)
    writer = ProactiveWriter(ProposalWriter(client))
    result = await writer.draft(CS, chat=CHAT_C, members=ROSTER, profiles=[], tasks=[], silenced_topics=[],
                                today=TODAY, decisions=list(decisions))
    return result, client


async def test_the_card_copies_choice_and_reason_from_the_log():
    # Even if the model wrote its own version of the choice, the card shows the log's.
    result, client = await draft(dict(ANSWER, choice="Side menu"))
    payload = result.payload
    assert result.kind is ProposalKind.REVISIT
    assert (payload.choice, payload.reason, payload.question) == (TABS.choice, TABS.reason, TABS.question)
    assert payload.new_point == "Ada finds the side menu cleaner"
    assert [o.label for o in payload.options] == ["Keep “Tabs”", "Reopen it"]
    assert [e.speaker for e in payload.context.evidence] == ["Ada", "Bruno"]
    assert "decision_id d1: App navigation -> chose \"Tabs\"" in client.calls[0]["messages"][-1]["content"]


async def test_an_unknown_decision_is_refused():
    bad = dict(ANSWER, decision_id="nope")
    result, _ = await draft(bad, bad)
    assert result is None


async def test_no_decisions_in_the_log_means_no_model_call():
    result, client = await draft(decisions=())
    assert result is None and client.calls == []


async def test_a_reopened_decision_is_not_offered_again():
    reopened = TABS.model_copy(update={"reopened_at": 200.0, "reopened_by": 1})
    result, client = await draft(decisions=(reopened,))
    assert result is None and client.calls == []


def payload() -> RevisitPayload:
    return RevisitPayload(context=context(CS, "Navigation reopened"), decision_id="d1", question=TABS.question,
                          choice=TABS.choice, reason=TABS.reason, decided_at=TABS.decided_at)


async def save_tabs(store):
    db = await store._connect()
    try:
        await add_decision(db, TABS)
    finally:
        await db.close()


async def test_keep_changes_nothing_and_records_who_kept_it(store):
    await save_tabs(store)
    service = ProposalService(store)
    card = await service.create(ProposalKind.REVISIT, payload(), rationale="r", origin_channel="group")
    result = await service.respond(card.proposal_id, action="choose", option=REVISIT_KEEP, member_id=2, participants=ROSTER)
    assert result.ok and "Kept" in result.message
    assert result.proposal.status is ProposalStatus.APPLIED and result.proposal.resolved_by == 2
    (decision,) = (await store.load_state()).decisions
    assert decision.reopened_at is None


async def test_reopen_marks_the_decision_and_keeps_its_history(store):
    await save_tabs(store)
    service = ProposalService(store)
    card = await service.create(ProposalKind.REVISIT, payload(), rationale="r", origin_channel="group")
    result = await service.respond(card.proposal_id, action="choose", option=REVISIT_REOPEN, member_id=1, participants=ROSTER)
    assert result.ok and "Reopened" in result.message
    (decision,) = (await store.load_state()).decisions
    assert decision.choice == "Tabs" and decision.reason == TABS.reason  # history kept
    assert decision.reopened_by == 1 and decision.reopened_at is not None
    # A second card on the same decision is stale.
    again = await service.create(ProposalKind.REVISIT, payload(), rationale="r", origin_channel="group")
    late = await service.respond(again.proposal_id, action="choose", option=REVISIT_REOPEN, member_id=2, participants=ROSTER)
    assert not late.ok and late.proposal.status is ProposalStatus.EXPIRED


@pytest.mark.parametrize("option", [None, 2])
async def test_a_wrong_option_changes_nothing(store, option):
    await save_tabs(store)
    service = ProposalService(store)
    card = await service.create(ProposalKind.REVISIT, payload(), rationale="r", origin_channel="group")
    result = await service.respond(card.proposal_id, action="choose", option=option, member_id=1, participants=ROSTER)
    assert not result.ok and result.proposal.status is ProposalStatus.PENDING


async def test_not_now_works_on_revisit_cards(store):
    await save_tabs(store)
    service = ProposalService(store)
    card = await service.create(ProposalKind.REVISIT, payload(), rationale="r", origin_channel="group")
    assert (await service.respond(card.proposal_id, action="not_now", member_id=1, participants=ROSTER)).ok


def test_r7_drops_a_card_on_a_decision_reopened_or_gone():
    card = ProactiveDraft(ProposalKind.REVISIT, payload(), "r")
    assert _dropped(card, [], [], [TABS]) == ""
    reopened = TABS.model_copy(update={"reopened_at": 5.0})
    assert _dropped(card, [], [], [reopened]).startswith("R7")
    assert _dropped(card, [], [], []).startswith("R7")


def test_memory_separates_reopened_decisions():
    reopened = TABS.model_copy(update={"reopened_at": 5.0})
    text = project_memory(ProjectState(decisions=[reopened]), ROSTER, TODAY)
    assert "Reopened by the group" in text and "(was “Tabs”)" in text
    assert "Decisions already taken" not in text


async def test_a_file_made_before_k3_gets_the_reopened_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE coord_decision (decision_id TEXT PRIMARY KEY, question TEXT NOT NULL, choice TEXT NOT NULL,"
        " reason TEXT NOT NULL DEFAULT '', decided_by INTEGER NOT NULL, decided_at REAL NOT NULL,"
        " source TEXT NOT NULL DEFAULT '')"
    )
    db.execute("INSERT INTO coord_decision VALUES ('d1','Nav','Tabs','fit',1,1.0,'')")
    db.commit()
    db.close()
    (decision,) = (await CoordinationStore(path).load_state()).decisions
    assert decision.choice == "Tabs" and decision.reopened_at is None
