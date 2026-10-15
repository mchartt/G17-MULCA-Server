from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..ports import Utterance

InterventionPath = Literal[
    "none",
    "strategy",
    "forced",
    "management_cycle",
    # A voice turn that ended but couldn't be transcribed (noise, provider
    # failure) -- distinct from "none" (judged, decided not to act), since
    # nothing was ever judged here at all. Logged so the audit trail shows
    # something happened, not silence, same as every other outcome.
    "untranscribed",
]


class EpisodicRecord(BaseModel):
    """
    One thing that happened, and why.

    Written even when nothing was said, so "the assistant chose to stay
    quiet" is as auditable as a reply.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: float
    conversation_id: str
    intervention_path: InterventionPath

    speaker: str = ""
    content: str = ""
    # "text" for a management_cycle row too -- not tied to one utterance's
    # modality, there's nothing truer to put here, and it keeps the column
    # non-null without inventing a third value nothing reads.
    modality: Literal["text", "audio"] = "text"
    trigger_variables: dict = Field(default_factory=dict)

    strategy_id: str | None = None
    prompt_fragment: str | None = None
    assistant_output: str | None = None

    # Management-cycle rows only. MULCA overloaded prompt_fragment and the
    # assistant's output for these; they get their own fields here.
    history_summary: str | None = None
    participant_roles_summary: str | None = None
    action_points: str | None = None


class MemorySummary(BaseModel):
    """
    A day summary, "today so far", or the overview (docs/MEMORY_DESIGN.md),
    as stored -- generated from the log, never from a previous summary.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["day", "today", "overview"]
    # The day it covers; for an overview, the day it was built.
    day: str
    content: dict
    # The latest log timestamp it covers.
    through_ts: float = 0.0
    created_at: float = 0.0


class StoredUtterance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker_id: int
    speaker_name: str
    content: str
    timestamp_ms: int
    modality: str = "text"

    @classmethod
    def of(cls, utterance: Utterance) -> "StoredUtterance":
        return cls(
            speaker_id=utterance.speaker_id,
            speaker_name=utterance.speaker_name,
            content=utterance.content,
            timestamp_ms=utterance.timestamp_ms,
            modality=utterance.modality,
        )

    def restore(self) -> Utterance:
        return Utterance(
            speaker_id=self.speaker_id,
            speaker_name=self.speaker_name,
            content=self.content,
            timestamp_ms=self.timestamp_ms,
            modality="audio" if self.modality == "audio" else "text",
        )


class MemorySnapshot(BaseModel):
    """
    Enough of a conversation to pick it up again after a restart.

    Not the audit trail: this answers "what should I remember right now",
    not "what happened and why".
    """

    model_config = ConfigDict(extra="forbid")

    history_summary: str = ""
    participant_roles_summary: str = ""
    action_points: str = ""
    history_tail: list[StoredUtterance] = Field(
        default_factory=list,
    )
    total_message_count: int = 0
    updated_at: float = 0.0

    # Cooldown bookkeeping (WorkingMemory.recent_actions/recent_action_at).
    # Found live: every reconnect (OpenAI's 60-minute Realtime limit, the
    # Watchdog's tier-1 recovery, a network blip) builds a brand-new
    # WorkingMemory and restored only the fields above -- total_message_count
    # carried over, but cooldown state silently reset to empty, so a
    # strategy that had just fired could immediately fire again right after
    # a reconnect, with no warning and no way to tell from the logs alone
    # that a reconnect was even the cause.
    recent_actions: dict[str, int] = Field(default_factory=dict)
    recent_action_at: dict[str, float] = Field(default_factory=dict)

    # CoalaPolicy._full_context_sent_at -- same reconnect-survival problem
    # as the cooldown fields above, for the same reason: a fresh
    # CoalaPolicy is built on every reconnect, so this gate (keyed on this
    # conversation_id -- one specific DM participant, not "any DM") would
    # otherwise reset to None every time, resending a group's summary once
    # per reconnect instead of truly once, or ignoring a DM's own
    # just-sent timestamp if it happened to reconnect within the hour.
    full_context_sent_at: float | None = None
