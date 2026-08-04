from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str


class AudioStreamMessage(IncomingMessage):
    event: Literal["audio_stream"]
    speaker: int
    is_notifying: bool = Field(alias="isNotifying")
    playback_time_ms: int = Field(alias="playbackTimeMs")
    audio: str


class PlaybackFrameMessage(IncomingMessage):
    event: Literal["playback_frame"]
    audio: str


class TextMessage(IncomingMessage):
    event: Literal["text_message"]
    text: str
    target_agent_guid: str | None = Field(
        default=None,
        alias="targetAgentGuid",
    )


class TriggerResponseMessage(IncomingMessage):
    event: Literal["trigger_response"]
    target_agent_guid: str | None = Field(
        default=None,
        alias="targetAgentGuid",
    )


class InterruptMessage(IncomingMessage):
    event: Literal["interrupt"]
    target_agent_guid: str | None = Field(
        default=None,
        alias="targetAgentGuid",
    )


class ConnectMainAssistantMessage(IncomingMessage):
    event: Literal["connect_main_assistant"]


class DisconnectMessage(IncomingMessage):
    event: Literal["disconnect"]


class CreateAuxiliaryAgentMessage(IncomingMessage):
    event: Literal["create_auxiliary_agent"]
    configuration: dict[str, Any] = Field(default_factory=dict)


class RemoveAuxiliaryAgentMessage(IncomingMessage):
    event: Literal["remove_auxiliary_agent"]
    agent_guid: str = Field(alias="agentGuid")


class CampaignSetupMessage(IncomingMessage):
    event: Literal["campaign_setup"]
    payload: dict[str, Any]


IncomingProtocolMessage = Annotated[
    AudioStreamMessage
    | PlaybackFrameMessage
    | TextMessage
    | TriggerResponseMessage
    | InterruptMessage
    | ConnectMainAssistantMessage
    | DisconnectMessage
    | CreateAuxiliaryAgentMessage
    | RemoveAuxiliaryAgentMessage
    | CampaignSetupMessage,
    Field(discriminator="event"),
]

incoming_message_adapter = TypeAdapter(IncomingProtocolMessage)


def parse_incoming_message(
    raw_message: str | bytes | dict[str, Any],
) -> IncomingProtocolMessage:
    return incoming_message_adapter.validate_json(
        raw_message
    ) if isinstance(raw_message, (str, bytes)) else (
        incoming_message_adapter.validate_python(raw_message)
    )