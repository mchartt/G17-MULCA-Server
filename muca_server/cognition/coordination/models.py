"""
The Group Coordinator's data: what the project state is made of.

Plain pydantic models, no database and no model calls here. Every other
module of the coordinator (store, proposals, writer, router) speaks in
these types, and the clients receive them as JSON (`to_wire()`).

Why pydantic: the code base already validates every protocol message with
it, and the same validation guards what the language model writes before
anything can become state (project rule: structured output, validated).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Short limits keep cards readable on Discord and Slack (one card, no wall
# of text) and stop a runaway model answer from filling the database.
MAX_TITLE = 200
MAX_TEXT = 2000


class _Model(BaseModel):
    """Common settings: unknown fields are an error, not silently kept."""

    model_config = ConfigDict(extra="forbid")

    def to_wire(self) -> dict[str, Any]:
        """The JSON-ready dict the clients receive (dates as ISO strings)."""
        return self.model_dump(mode="json")


# ----------------------------------------------------------------------
# Members
# ----------------------------------------------------------------------


class MemberProfile(_Model):
    """
    One member of the group. Only what the project rules allow us to keep:
    display name, discipline, skills, weekly hours (no scores, no judgements).
    """

    member_id: int  # the same id as protocol Participant.id
    display_name: str = Field(min_length=1, max_length=100)
    discipline: str = Field(default="", max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=20)
    weekly_hours: int | None = Field(default=None, ge=0, le=80)


# ----------------------------------------------------------------------
# Charter and plan
# ----------------------------------------------------------------------


class MilestoneDraft(_Model):
    """A milestone as proposed, before it has an id in the database."""

    title: str = Field(min_length=1, max_length=MAX_TITLE)
    due_date: date | None = None


class Milestone(MilestoneDraft):
    milestone_id: str


class Charter(_Model):
    """The project's goal and deliverables, as the group confirmed them."""

    goal: str = Field(min_length=1, max_length=MAX_TEXT)
    deliverables: list[str] = Field(default_factory=list, max_length=20)
    confirmed_by: int
    confirmed_at: float  # unix time


class TaskStatus(StrEnum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    BLOCKED = "blocked"


class Task(_Model):
    task_id: str
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    owner_id: int | None = None
    due_date: date | None = None
    status: TaskStatus = TaskStatus.TODO
    depends_on: list[str] = Field(default_factory=list)  # other task_ids


class Decision(_Model):
    """One closed decision and why, so "why did we decide X?" has an answer."""

    decision_id: str
    question: str = Field(min_length=1, max_length=MAX_TITLE)
    choice: str = Field(min_length=1, max_length=MAX_TITLE)
    reason: str = Field(default="", max_length=MAX_TEXT)
    decided_by: int
    decided_at: float
    # Where the decision came from, e.g. "proposal:<id>", for the answer's source.
    source: str = ""


class ProjectState(_Model):
    """Everything the project panel shows, in one object."""

    charter: Charter | None = None
    members: list[MemberProfile] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------


class ProposalKind(StrEnum):
    """What a proposal would change once confirmed. M1 has the charter only."""

    CHARTER = "charter"


class ProposalStatus(StrEnum):
    """
    Life cycle: PENDING -> APPLIED | REJECTED | EXPIRED. The last three are
    final: a proposal never goes back to PENDING (proposals.py enforces it).
    """

    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CharterPayload(_Model):
    """What a charter proposal contains (written by the model, validated here)."""

    goal: str = Field(min_length=1, max_length=MAX_TEXT)
    deliverables: list[str] = Field(default_factory=list, max_length=20)
    milestones: list[MilestoneDraft] = Field(default_factory=list, max_length=20)

    @field_validator("deliverables")
    @classmethod
    def _no_empty_deliverables(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if any(len(item) > MAX_TITLE for item in cleaned):
            raise ValueError("deliverable too long")
        return cleaned


# Which payload model belongs to which kind. A new kind = one new line here,
# one payload model above and one applier in proposals.py.
PAYLOAD_MODELS: dict[ProposalKind, type[_Model]] = {
    ProposalKind.CHARTER: CharterPayload,
}


class Proposal(_Model):
    """
    A change the coordinator suggests. It changes nothing until a member of
    the group confirms it (project rule: the model never writes state).
    """

    proposal_id: str
    kind: ProposalKind
    status: ProposalStatus = ProposalStatus.PENDING
    payload: dict[str, Any]
    # One sentence: why the coordinator is suggesting this.
    rationale: str = Field(default="", max_length=500)
    origin_channel: str = "discord"
    created_at: float
    expires_at: float
    resolved_by: int | None = None
    resolved_at: float | None = None

    def typed_payload(self) -> _Model:
        """The payload validated against its kind's model."""
        return PAYLOAD_MODELS[self.kind].model_validate(self.payload)
