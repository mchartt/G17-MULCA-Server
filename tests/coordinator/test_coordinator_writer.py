"""ProposalWriter: structured call shape, validation, one retry, then silence."""

from __future__ import annotations

from datetime import date

from muca_server.cognition.coordination.writer import (
    CALL_TIMEOUT_SECONDS,
    CHARTER_SCHEMA,
    DEFAULT_WRITER_MODEL,
    ProposalWriter,
)

from .conftest import GOOD_CHARTER, FakeChatClient

TODAY = date(2026, 11, 15)


async def test_a_good_answer_becomes_a_validated_draft():
    client = FakeChatClient(GOOD_CHARTER)
    costs = []
    writer = ProposalWriter(client, record_cost=lambda *args: costs.append(args))

    draft = await writer.draft_charter("Deliver a report by 2 Dec.", today=TODAY)

    assert draft.payload.goal == GOOD_CHARTER["goal"]
    assert draft.payload.milestones[0].due_date == date(2026, 12, 2)
    assert draft.rationale.startswith("Stepping in because")
    # Same call shape as the lab's summaries: strict json_schema, model name.
    (call,) = client.calls
    assert call["model"] == DEFAULT_WRITER_MODEL
    assert call["response_format"] == {"type": "json_schema", "json_schema": CHARTER_SCHEMA}
    assert call["timeout"] == CALL_TIMEOUT_SECONDS  # never the SDK's 10 minutes
    # The assignment is passed as quoted data, never as instructions.
    assert "<assignment>\nDeliver a report by 2 Dec.\n</assignment>" in call["messages"][1]["content"]
    assert "2026-11-15" in call["messages"][1]["content"]
    # Cost recorded as (text in, cached, audio in, cached audio, text out, audio out).
    assert costs == [("coordinator", DEFAULT_WRITER_MODEL, (90, 10, 0, 0, 50, 0))]


async def test_an_invalid_answer_is_retried_once():
    bad = dict(GOOD_CHARTER, milestones=[{"title": "X", "due_date": "next friday"}])
    client = FakeChatClient(bad, GOOD_CHARTER)
    draft = await ProposalWriter(client).draft_charter("text", today=TODAY)
    assert draft is not None
    assert len(client.calls) == 2


async def test_two_failures_give_none_and_never_raise():
    client = FakeChatClient("not json at all", RuntimeError("provider down"))
    assert await ProposalWriter(client).draft_charter("text", today=TODAY) is None
    assert len(client.calls) == 2


async def test_an_empty_goal_is_not_accepted():
    client = FakeChatClient(dict(GOOD_CHARTER, goal=""), dict(GOOD_CHARTER, goal=""))
    assert await ProposalWriter(client).draft_charter("text", today=TODAY) is None


async def test_a_rationale_that_is_empty_or_over_60_words_costs_a_retry():
    too_long = dict(GOOD_CHARTER, rationale="word " * 61)
    client = FakeChatClient(dict(GOOD_CHARTER, rationale=""), too_long)
    assert await ProposalWriter(client).draft_charter("text", today=TODAY) is None
    assert len(client.calls) == 2
