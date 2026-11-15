"""Builders shared by the M3 tests (proactive cards). No network, no model."""

from __future__ import annotations

from muca_server.cognition.coordination.models import (
    DecisionOption,
    DecisionPayload,
    Evidence,
    ProactiveContext,
    TaskAssignmentPayload,
)

ROSTER = {1: "Ada", 2: "Bruno", 3: "Chiara"}


def context(strategy_id: str = "cs5_owner_and_date", topic: str = "Evaluation protocol") -> ProactiveContext:
    return ProactiveContext(
        strategy_id=strategy_id,
        topic=topic,
        evidence=[Evidence(speaker="Ada", text="Someone has to write the protocol", at_ms=1)],
    )


def task_payload(**changes) -> TaskAssignmentPayload:
    data = dict(context=context(), title="Write the evaluation protocol", owner_id=2, due_date="2026-11-27")
    data.update(changes)
    return TaskAssignmentPayload(**data)


def delegation_payload(**changes) -> TaskAssignmentPayload:
    data = dict(
        context=context("cs2_delegate_simple_task", "Fonts and margins"),
        title="Fix fonts and margins of the report",
        return_to="Evaluation protocol",
    )
    data.update(changes)
    return TaskAssignmentPayload(**data)


def decision_payload() -> DecisionPayload:
    return DecisionPayload(
        context=context("cs3_close_decision", "Test method"),
        question="Which usability test do we run?",
        options=[
            DecisionOption(label="Think-aloud", argument="Shows why users get stuck (Sofia)."),
            DecisionOption(label="SUS questionnaire", argument="Fast, but only a score (Luca)."),
        ],
    )
