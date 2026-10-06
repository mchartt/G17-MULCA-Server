import json
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class IncomingMessage(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )


class TargetedMessage(IncomingMessage):
    """
    Base for messages addressed to one agent.

    An empty or missing targetAgentGuid means the main agent.
    """

    target_agent_guid: str | None = Field(
        default=None,
        alias="targetAgentGuid",
    )


class AudioStreamMessage(IncomingMessage):
    event: Literal["audio_stream"]
    speaker: int
    is_notifying: bool = Field(alias="isNotifying")
    playback_time_ms: int = Field(alias="playbackTimeMs")
    audio: str


class PlaybackFrameMessage(IncomingMessage):
    event: Literal["playback_frame"]
    audio: str


class TextMessage(TargetedMessage):
    """Inject text into one agent's context. Not a user utterance."""

    event: Literal["text_message"]
    text: str
    role: str = "system"


class UserTextMessage(IncomingMessage):
    """
    One user's utterance, in text.

    The text-modality sibling of AudioStreamMessage: it carries who spoke,
    so it drives turn taking the same way audio does.
    """

    event: Literal["user_text"]
    speaker: int
    speaker_name: str = Field(default="", alias="speakerName")
    text: str


class Participant(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    id: int
    name: str


class ParticipantsMessage(IncomingMessage):
    """
    Who is present, including anyone who has not spoken yet.

    Pushed by the client rather than asked for, so the server never has to
    make a request back down the socket mid-turn.
    """

    event: Literal["participants"]
    participants: list[Participant] = Field(
        default_factory=list,
    )
    # Which roster this is: the text channel's, or the voice call's --
    # tracked completely separately server-side (Conversation keeps one
    # dict per modality), since who's in one is not reliably who's in the
    # other. Defaults to "text" so an older/other client that only ever
    # sent one combined roster keeps behaving exactly as before.
    modality: Literal["text", "audio"] = "text"


class TriggerResponseMessage(TargetedMessage):
    event: Literal["trigger_response"]
    max_tokens: int | None = Field(
        default=None,
        alias="maxTokens",
    )
    interrupt: bool = True
    instructions: str = ""


class InterruptMessage(TargetedMessage):
    event: Literal["interrupt", "interrupt_response"]


class ConnectAgentMessage(TargetedMessage):
    event: Literal["connect_main_assistant", "connect"]

    # Which conversation this session continues, for memory that outlives it.
    conversation_id: str | None = Field(
        default=None,
        alias="conversationId",
    )

    # A private conversation is never written to long-term memory.
    is_dm: bool = Field(default=False, alias="isDm")

    # For a DM only: the group conversation_id its sender is a recognized
    # participant of, so that DM gets one-way read access to that group's
    # memory. None if the sender isn't recognized as belonging to any
    # instance -- the client (e.g. mulca_discord) decides this, since it's
    # the one that knows what a "participant" or "instance" is.
    linked_conversation_id: str | None = Field(
        default=None,
        alias="linkedConversationId",
    )


class DisconnectMessage(TargetedMessage):
    event: Literal["disconnect"]


class CreateAuxiliaryAgentMessage(IncomingMessage):
    event: Literal["create_auxiliary_agent"]
    auxiliary_agent_type: str = Field(alias="auxiliaryAgentType")


class RemoveAuxiliaryAgentMessage(TargetedMessage):
    event: Literal["remove_auxiliary_agent"]


IncomingProtocolMessage = Annotated[
    AudioStreamMessage
    | PlaybackFrameMessage
    | TextMessage
    | UserTextMessage
    | ParticipantsMessage
    | TriggerResponseMessage
    | InterruptMessage
    | ConnectAgentMessage
    | DisconnectMessage
    | CreateAuxiliaryAgentMessage
    | RemoveAuxiliaryAgentMessage,
    Field(discriminator="event"),
]

class RawProviderMessage(TargetedMessage):
    """
    A frame with no `event`: a provider payload the client wants passed
    through as-is (the Unity client's SendAnythingAsync, e.g. the
    session.update JSON it sends right after connecting).

    Not part of the discriminated union, since it has no discriminator.
    """

    event: Literal["raw_provider_message"] = "raw_provider_message"
    payload: dict[str, Any] = Field(default_factory=dict)


CONNECT_EVENTS = frozenset(
    {"connect_main_assistant", "connect"}
)

incoming_message_adapter = TypeAdapter(IncomingProtocolMessage)


def parse_incoming_message(
    raw_message: str | bytes | dict[str, Any],
) -> IncomingProtocolMessage | RawProviderMessage:
    data = (
        json.loads(raw_message)
        if isinstance(raw_message, (str, bytes))
        else raw_message
    )

    if isinstance(data, dict) and "event" not in data:
        return RawProviderMessage(
            target_agent_guid=data.get("targetAgentGuid"),
            payload=data,
        )

    return incoming_message_adapter.validate_python(data)
