from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class OutgoingMessage(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)


class StateUpdateMessage(OutgoingMessage):
    event: Literal["state_update"] = "state_update"
    is_connected: bool = Field(serialization_alias="isConnected")
    current_speaker: int = Field(serialization_alias="currentSpeaker")
    last_speaker: int = Field(serialization_alias="lastSpeaker")
    is_answering: bool = Field(serialization_alias="isAnswering")
    assistant_spoke_last: bool = Field(
        serialization_alias="assistantSpokeLast"
    )
    clear_audio_queue: bool = Field(
        serialization_alias="clearAudioQueue"
    )
    audio: str = ""

    @classmethod
    def audio_chunk(
        cls,
        *,
        current_speaker: int,
        last_speaker: int,
        encoded_audio: str,
        is_answering: bool = True,
        assistant_spoke_last: bool = True,
    ) -> "StateUpdateMessage":
        # is_answering follows the assistant's turn (playback start to
        # playback drain), not the chunks: the first chunks of a reply are
        # generated before the client has played anything.
        return cls(
            is_connected=True,
            current_speaker=current_speaker,
            last_speaker=last_speaker,
            is_answering=is_answering,
            assistant_spoke_last=assistant_spoke_last,
            clear_audio_queue=False,
            audio=encoded_audio,
        )

    @classmethod
    def response_completed(
        cls,
        *,
        current_speaker: int,
        last_speaker: int,
    ) -> "StateUpdateMessage":
        return cls(
            is_connected=True,
            current_speaker=current_speaker,
            last_speaker=last_speaker,
            is_answering=False,
            assistant_spoke_last=True,
            clear_audio_queue=False,
            audio="",
        )

    @classmethod
    def interrupted(
        cls,
        *,
        current_speaker: int,
        last_speaker: int,
    ) -> "StateUpdateMessage":
        return cls(
            is_connected=True,
            current_speaker=current_speaker,
            last_speaker=last_speaker,
            is_answering=False,
            assistant_spoke_last=True,
            clear_audio_queue=True,
            audio="",
        )
        
class AgentMessage(OutgoingMessage):
    event: Literal["agent_message"] = "agent_message"

    # The client reads this field as "guid"; absent means the main agent.
    agent_guid: str | None = Field(
        default=None,
        serialization_alias="guid",
    )
    text: str


class AgentGuidMessage(OutgoingMessage):
    event: Literal["agent_guid"] = "agent_guid"

    agent_guid: str = Field(serialization_alias="guid")


class AssistantTextMessage(OutgoingMessage):
    """
    What the main agent is saying, in text.

    Deltas arrive as the agent produces them; one final frame closes the
    turn so a client knows it can stop editing.
    """

    event: Literal["assistant_text"] = "assistant_text"

    text: str
    is_final: bool = Field(
        default=False,
        serialization_alias="isFinal",
    )

    # "text": this is the reply itself. "audio": this is the transcript of a
    # reply that is being spoken, and its audio travels in state_update.
    modality: Literal["text", "audio"] = "text"

    # Only meaningful on the final frame: the reply was cut short.
    interrupted: bool = False

    @classmethod
    def delta(cls, text: str) -> "AssistantTextMessage":
        return cls(text=text, is_final=False)

    @classmethod
    def final(cls) -> "AssistantTextMessage":
        return cls(text="", is_final=True)


class ErrorMessage(OutgoingMessage):
    """
    A client message could not be carried out.

    The session stays open; clients that do not know this event ignore it.
    """

    event: Literal["error"] = "error"

    message: str
    agent_guid: str | None = Field(
        default=None,
        serialization_alias="targetAgentGuid",
    )


class ListeningStateMessage(OutgoingMessage):
    """
    "toggle"/"transcribe" modes: sent right after a window opens or
    closes (a button/`/listen` toggle, or a "transcribe"-mode wake phrase
    opening or auto-closing one) -- the client never sends one itself; the
    server is the only side that knows the real state for sure.
    """

    event: Literal["listening_state"] = "listening_state"
    open: bool


class TurnSkippedMessage(OutgoingMessage):
    """
    This turn's judgment concluded no strategy should fire -- there is no
    reply coming for it at all. Lets a client stop showing a "typing"
    indicator (or any other "a reply is coming" UI) immediately instead of
    waiting out its own timeout for a reply that was never going to arrive.
    Clients that do not know this event ignore it.
    """

    event: Literal["turn_skipped"] = "turn_skipped"


class UsageMessage(OutgoingMessage):
    """
    What one OpenAI call made for this session cost, by the server's own
    estimate (agents/openai/main_agent.py, record_cost) -- for a client that
    limits spending. Clients that do not know this event ignore it.
    """

    event: Literal["usage"] = "usage"

    # What made the call: "main", "intervention", "transcription",
    # "summary" or "embedding".
    agent: str
    fresh_text: int = Field(serialization_alias="freshText")
    cached_text: int = Field(serialization_alias="cachedText")
    fresh_audio: int = Field(serialization_alias="freshAudio")
    cached_audio: int = Field(serialization_alias="cachedAudio")
    out_text: int = Field(serialization_alias="outText")
    out_audio: int = Field(serialization_alias="outAudio")
    cost_usd: float = Field(serialization_alias="costUsd")


OutgoingProtocolMessage = (
    StateUpdateMessage
    | AgentMessage
    | AgentGuidMessage
    | AssistantTextMessage
    | ErrorMessage
    | ListeningStateMessage
    | TurnSkippedMessage
    | UsageMessage
)

def serialize_outgoing_message(
    message: OutgoingProtocolMessage,
) -> str:
    return message.model_dump_json(
        by_alias=True,
        exclude_none=True,
    )