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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    display name, discipline, skills, weekly hours, and a few lines the member
    writes about their own background (no scores, no judgements).
    """

    member_id: int  # the same id as protocol Participant.id
    display_name: str = Field(min_length=1, max_length=100)
    discipline: str = Field(default="", max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=20)
    weekly_hours: int | None = Field(default=None, ge=0, le=80)
    # Written by the member, in their own words: what they studied, what they
    # have done before. The coordinator reads it when it suggests an owner.
    background: str = Field(default="", max_length=600)


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
    # Where each link came from (task_id it waits on -> "card <id>: <chat line>",
    # "by hand (<member>)"); a link missing here has no known origin.
    dependency_sources: dict[str, str] = Field(default_factory=dict)
    # Written by a member by hand (the "New task" popup); cards never set it.
    description: str = Field(default="", max_length=MAX_TEXT)
    # How far along it is, 0-100, set by a member; 100 once done.
    progress: int = Field(default=0, ge=0, le=100)
    # Unix times kept by the store (TASK_TIMES): when it was made, when it was done.
    created_at: float | None = None
    done_at: float | None = None


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
    # K3: set when a member chose "Reopen it" on a revisit card. The decision
    # stays in the log (history is kept); a later CS3 card can close it again.
    reopened_at: float | None = None
    reopened_by: int | None = None


# A project document: kept as text so Cadence can read it when it answers
# (Project settings > Documents). The file itself is never kept.
MAX_DOCUMENT_CHARS = 20_000
MAX_DOCUMENTS = 20


class ProjectDocument(_Model):
    """What the panel lists about a document; its text stays on the server."""

    document_id: str
    name: str = Field(min_length=1, max_length=MAX_TITLE)
    chars: int = 0
    added_by: int | None = None
    added_at: float = 0.0


class ProjectState(_Model):
    """Everything the project panel shows, in one object."""

    charter: Charter | None = None
    members: list[MemberProfile] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    documents: list[ProjectDocument] = Field(default_factory=list)
    # The language the group chose for the coordinator's texts: "en", "it",
    # or "" (not chosen: it follows the chat). Command "language".
    language: str = ""


# ----------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------


class ProposalKind(StrEnum):
    """What a proposal would change once confirmed."""

    CHARTER = "charter"  # M1: kickoff (UC2)
    TASK_OWNER = "task_owner"  # M3: CS5, an agreed action gets an owner and a date
    DELEGATION = "delegation"  # M3: CS2, one member takes a simple task
    DECISION = "decision"  # M3: CS3, the group picks one option, with its reason
    PLAN = "plan"  # M4: /planning, the tasks the members wrote with suggested owners
    CHECKIN = "checkin"  # M4: /checkin, up to three ways to fix what is late or at risk
    BLOCKER = "blocker"  # M4: CS7, a task is blocked or waits on another
    APPROACH = "approach"  # plan opener (CS-OPEN-01): how the group plans the work, as it said it
    REVISIT = "revisit"  # K3: a decision in the log is being reopened: keep it or reopen it


class ProposalStatus(StrEnum):
    """
    Life cycle: PENDING -> APPLIED | REJECTED | EXPIRED | SNOOZED. The last
    four are final: a proposal never goes back to PENDING (proposals.py
    enforces it). SNOOZED = a member pressed "Not now" on a proactive card.
    """

    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SNOOZED = "snoozed"


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


# ----------------------------------------------------------------------
# Proactive proposals (M3): cards the coordinator posts on its own
# ----------------------------------------------------------------------

MAX_TOPIC = 120
MAX_QUOTE = 300


class Evidence(_Model):
    """
    One chat message that made a strategy fire, copied from the session's
    own history by the server (proactive.py), never written by the model:
    the model only points at message numbers. Shown under "Why?".
    """

    speaker: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=MAX_QUOTE)
    at_ms: int = 0


class ProactiveContext(_Model):
    """
    Why a proactive card exists: which strategy fired, about what topic
    (the key of "Not now"), and the messages behind it.
    """

    strategy_id: str = Field(min_length=1, max_length=60)
    topic: str = Field(min_length=1, max_length=MAX_TOPIC)
    evidence: list[Evidence] = Field(min_length=1, max_length=5)


class TaskAssignmentPayload(_Model):
    """
    CS5 (task_owner) and CS2 (delegation): a task and who takes it.

    task_id: an existing task without an owner (CS5), or None for a new task.
    owner_id: the member the coordinator suggests (CS5 only; CS2 never
    suggests anyone, spec S2 "do not assign"). Any member can still press
    "I'll take it" and become the owner instead.
    return_to: CS2 only, the complex topic the group should go back to.
    """

    context: ProactiveContext
    task_id: str | None = None
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    owner_id: int | None = None
    due_date: date | None = None
    return_to: str = Field(default="", max_length=MAX_TITLE)


class DecisionOption(_Model):
    """One option of a decision card, with what was said for it in the chat."""

    label: str = Field(min_length=1, max_length=MAX_TITLE)
    argument: str = Field(default="", max_length=MAX_QUOTE)


class DecisionPayload(_Model):
    """CS3: the open question and its 2 or 3 options (one button each)."""

    context: ProactiveContext
    question: str = Field(min_length=1, max_length=MAX_TITLE)
    options: list[DecisionOption] = Field(min_length=2, max_length=3)


# ----------------------------------------------------------------------
# M4: planning, check-in, blockers
# ----------------------------------------------------------------------


class PlanItem(_Model):
    """One task the members wrote, with the owner the coordinator suggests and why."""

    title: str = Field(min_length=1, max_length=MAX_TITLE)
    owner_id: int | None = None
    reason: str = Field(default="", max_length=MAX_QUOTE)
    due_date: date | None = None
    # "Find owners" (planning with args.owners): the plan task this item gives
    # an owner to; None = a new task (the usual /planning list).
    task_id: str | None = None


class PlanPayload(_Model):
    """/planning: the whole list is confirmed (or rejected) with one click."""

    items: list[PlanItem] = Field(min_length=1, max_length=15)


class CheckinOption(_Model):
    """
    One way to fix a risk found by the check-in: change one task (new date,
    new owner, or drop it). At least one change is required.
    """

    label: str = Field(min_length=1, max_length=MAX_TITLE)
    task_id: str = Field(min_length=1, max_length=40)
    new_due_date: date | None = None
    new_owner_id: int | None = None
    drop: bool = False

    @model_validator(mode="after")
    def _one_change(self) -> "CheckinOption":
        if not self.drop and self.new_due_date is None and self.new_owner_id is None:
            raise ValueError("an option must change the date, the owner, or drop the task")
        return self


class CheckinPayload(_Model):
    """
    /checkin: what the server found comparing plan and dates (`findings`,
    computed in code by checkin.py, never by the model) and up to 3 options.
    """

    findings: list[str] = Field(min_length=1, max_length=10)
    options: list[CheckinOption] = Field(min_length=1, max_length=3)


class BlockerCertainty(StrEnum):
    """
    How sure the coordinator is (CS7 v2). The card says it in words, so the
    group can tell a fact it stated from something the coordinator inferred.

    BLOCKED   a member said they are stuck or waiting on other work.
    POSSIBLE  nobody said so: the chat and the plan suggest a dependency
              (Sara tests on Friday, the API she needs is not done). The
              card asks the group to confirm it before anything is recorded.
    """

    BLOCKED = "blocked"
    POSSIBLE = "possible"


MAX_WAY = 160


class BlockerPayload(_Model):
    """
    CS7: a task is blocked, or waits on another one. `helper_id` is who could help.

    Confirming a BLOCKED card marks the task blocked and records what it
    waits on. Confirming a POSSIBLE card only records the dependency ("yes,
    it depends"): the task is not marked blocked, nobody said it was.
    The work it waits on is a plan task (`depends_on_task_id`) or, when the
    plan does not have it yet, its title as said in the chat
    (`depends_on_title`), added to the plan on confirmation.
    `ways_forward`: up to three concrete options taken from the chat and the
    plan (mock data, a minimal version first, another order). Shown on the
    card as suggestions: choosing one changes nothing by itself.
    `depends_on_owner_id`: when the work it waits on is new, the member the
    chat names as the one who delivers it ("Mathias must send me the data");
    it becomes that new task's owner on confirmation. Never a guess.
    """

    context: ProactiveContext
    task_id: str | None = None
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    depends_on_task_id: str | None = None
    depends_on_title: str = Field(default="", max_length=MAX_TITLE)
    depends_on_owner_id: int | None = None
    helper_id: int | None = None
    question: str = Field(default="", max_length=MAX_QUOTE)
    certainty: BlockerCertainty = BlockerCertainty.BLOCKED
    ways_forward: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("ways_forward")
    @classmethod
    def _short_ways(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value if item.strip()]
        if any(len(item) > MAX_WAY for item in cleaned):
            raise ValueError("a way forward is too long")
        return cleaned

    @model_validator(mode="after")
    def _possible_names_what(self) -> "BlockerPayload":
        if (
            self.certainty is BlockerCertainty.POSSIBLE
            and self.depends_on_task_id is None
            and not self.depends_on_title.strip()
        ):
            raise ValueError("a possible dependency must say what the task may wait on")
        return self


class RevisitPayload(_Model):
    """
    K3: the chat reopens a question the decision log already closed.
    The card recalls the decision and its reason, says what seems new
    (`new_point`, "" if nothing new was said) and offers two buttons:
    option 0 "Keep" (nothing changes) and option 1 "Reopen it" (the decision
    is marked reopened, history kept). `choice` and `reason` are copied by
    the server from the log, never written by the model.
    """

    context: ProactiveContext
    decision_id: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=MAX_TITLE)
    choice: str = Field(min_length=1, max_length=MAX_TITLE)
    reason: str = Field(default="", max_length=MAX_TEXT)
    decided_at: float = 0
    new_point: str = Field(default="", max_length=MAX_QUOTE)
    # The two buttons, labelled by the server (Discord and Slack draw one
    # button per option, as for a decision card): [Keep "<choice>", Reopen it].
    options: list[DecisionOption] = Field(default_factory=list, max_length=2)


REVISIT_KEEP = 0
REVISIT_REOPEN = 1


class ApproachPayload(_Model):
    """
    Plan opener (CS-OPEN-01): after the charter, how the group said it wants
    to plan the work. Each agreement rests on chat lines the server copied
    into `context.evidence`; confirming it writes one decision.
    """

    context: ProactiveContext
    question: str = Field(min_length=1, max_length=MAX_TITLE)
    summary: str = Field(min_length=1, max_length=MAX_TITLE)
    agreements: list[str] = Field(min_length=1, max_length=5)
    open_points: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("agreements", "open_points")
    @classmethod
    def _short_points(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value if item.strip()]
        if any(len(item) > MAX_TITLE for item in cleaned):
            raise ValueError("point too long")
        return cleaned


# Which payload model belongs to which kind. A new kind = one new line here,
# one payload model above and one applier in appliers.py.
PAYLOAD_MODELS: dict[ProposalKind, type[_Model]] = {
    ProposalKind.CHARTER: CharterPayload,
    ProposalKind.TASK_OWNER: TaskAssignmentPayload,
    ProposalKind.DELEGATION: TaskAssignmentPayload,
    ProposalKind.DECISION: DecisionPayload,
    ProposalKind.PLAN: PlanPayload,
    ProposalKind.CHECKIN: CheckinPayload,
    ProposalKind.BLOCKER: BlockerPayload,
    ProposalKind.APPROACH: ApproachPayload,
    ProposalKind.REVISIT: RevisitPayload,
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
