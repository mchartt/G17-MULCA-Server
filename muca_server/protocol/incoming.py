import json
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# Group Coordinator messages (added, nothing existing changed).
from .coordinator import CoordinatorCommandMessage, ProposalResponseMessage


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


class Attachment(IncomingMessage):
    """The text a client read out of a file attached to a message."""

    name: str
    text: str = ""
    # The whole document's length; more than len(text) when it was cut.
    chars: int = 0
    # False: attached, but nothing could be read (an image, a scan).
    readable: bool = True


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
    # The client knows its own @mention ("<@1557...>"); the text alone doesn't
    # say whose id that is.
    mentions_assistant: bool = Field(default=False, alias="mentionsAssistant")
    attachments: list[Attachment] = Field(default_factory=list)


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

    # Realtime models this session uses instead of the server's, e.g. a
    # model chosen for one bot. Empty means the server's setting.
    main_model: str = Field(default="", alias="mainModel")
    intervention_model: str = Field(default="", alias="interventionModel")

    # Which bot/team this session belongs to (e.g. "MULCA-group3"), purely
    # for cost-report attribution (usage_report.py) -- conversation_id alone
    # ("channel:<id>") never reveals that on its own, since one deployment
    # runs many bots against this one shared server. Empty for a client that
    # doesn't send it (an older client, a non-Discord one): usage then falls
    # back to attributing by conversation_id instead.
    bot_name: str = Field(default="", alias="botName")

    # Per-link override of this server's own MUCA_LISTENING_MODE default
    # (run_server.py) -- None means use the server's setting, same
    # precedence as main_model/intervention_model above. Lets different
    # teams run different modes without a server-wide redeploy.
    listening_mode: (
        Literal["ambient", "toggle", "transcribe", "wake", "wake_v2"] | None
    ) = Field(
        default=None,
        alias="listeningMode",
    )


class SetModelMessage(IncomingMessage):
    """
    Admin: change the server's realtime model for every conversation.

    Every live session is then ended with 1012, so clients reopen on it.
    """

    event: Literal["set_model"]
    admin_token: str = Field(alias="adminToken")
    model: str
    # Also change the intervention agent's model, not only the main agent's.
    include_intervention: bool = Field(
        default=False,
        alias="includeIntervention",
    )


class SetCostAlertMessage(IncomingMessage):
    """
    Admin: change this MUCA-Server's cost-alert webhook/threshold.

    Unlike set_model, no live session needs to restart for this -- it only
    affects where/when usage_report.py posts, not anything a connected
    agent is doing. Either field left unset keeps that setting as it was.
    """

    event: Literal["set_cost_alert"]
    admin_token: str = Field(alias="adminToken")
    webhook_url: str | None = Field(default=None, alias="webhookUrl")
    threshold_usd: float | None = Field(default=None, alias="thresholdUsd")


class ArmListeningMessage(IncomingMessage):
    """
    "toggle" mode only: a participant pressed the button/`/listen` -- a
    toggle, same message opens and closes a window (the intervention agent
    judges turns, same as always, only while one is open). No-op in
    ambient or "transcribe" mode. No admin token -- any participant may
    send this.
    """

    event: Literal["arm_listening"]


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
    | RemoveAuxiliaryAgentMessage
    | SetModelMessage
    | SetCostAlertMessage
    | ArmListeningMessage
    | ProposalResponseMessage
    | CoordinatorCommandMessage,
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
