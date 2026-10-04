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
    history_tail: list[StoredUtterance] = Field(
        default_factory=list,
    )
    total_message_count: int = 0
    updated_at: float = 0.0
