from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

INTERVENTION_DETECTION = "intervention_detection"
DEFAULT_AUXILIARY = "default"

# The Unity client's AuxiliaryAgentType enum names, as it sends them.
AUXILIARY_AGENT_ALIASES = {
    "InterventionDetectionAgentOpenAI": INTERVENTION_DETECTION,
    "TestableInterventionDetectionAgentOpenAI": INTERVENTION_DETECTION,
    "InterventionDetectionGemini": INTERVENTION_DETECTION,
    "DefaultAgentOpenAI": DEFAULT_AUXILIARY,
    "DefaultAgentGemini": DEFAULT_AUXILIARY,
}

MAIN_AGENT_ID = 0


class AgentProvider(StrEnum):
    NONE = "none"
    OPENAI = "openai"
    GEMINI = "gemini"


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """
    What a provider does on its own, negotiated when the agent connects.

    The session branches its turn taking on these: with `vad` the provider
    reports user speech itself, and with `interrupt` it stops talking when a
    user speaks without being told to.
    """

    vad: bool = False
    interrupt: bool = False


class AgentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AgentProvider = AgentProvider.NONE
    model: str = ""
    api_key: str | None = None
    instructions: str = ""
    session_config: dict[str, Any] = Field(
        default_factory=dict,
    )


class MainAgentOptions(AgentOptions):
    name: str = "ASSISTANT"
    agent_id: int = MAIN_AGENT_ID
    # The conversation's mode, as the handshake named it ("wake_v2"
    # changes the transcription prompt).
    listening_mode: str = ""


class AuxiliaryAgentOptions(AgentOptions):
    agent_guid: UUID | None = None
    agent_type: str = "default"
    listen_to_assistant: bool = False

    # None means every user.
    listened_users: list[int] | None = None

    # Whether *this session's* conversation is a private DM -- read off the
    # conversation at creation time, not fixed per deployment, since one
    # server handles both group and DM sessions with the same factory.
    is_dm: bool = False

    # This conversation's own resolved mode ("ambient"/"toggle"/
    # "transcribe" -- Conversation's own listening_mode, after any per-link
    # handshake override), read off the conversation at creation time, same
    # as is_dm above -- one server handles every link's own mode with the
    # same factory. OpenAIAgentFactory.build_auxiliary uses this to decide
    # which strategy catalog the intervention agent is built with: a
    # structured enum (see its _tool_definition), not just prose, so
    # whichever strategies aren't in that conversation's own catalog are
    # structurally impossible for the model to select, not merely
    # discouraged by a trigger_description it might misjudge.
    listening_mode: str = "ambient"

    @classmethod
    def for_type(
        cls,
        auxiliary_agent_type: str,
        *,
        is_dm: bool = False,
    ) -> "AuxiliaryAgentOptions":
        """
        Build options for one of the agent types the client can ask for.

        Accepts MUCA's own names and the Unity client's AuxiliaryAgentType
        enum names, which it sends verbatim.
        """

        agent_type = AUXILIARY_AGENT_ALIASES.get(
            auxiliary_agent_type,
            auxiliary_agent_type,
        )

        if agent_type == INTERVENTION_DETECTION:
            return cls(
                agent_type=INTERVENTION_DETECTION,
                # The "testable" Unity variant does not hear the assistant.
                listen_to_assistant=(
                    auxiliary_agent_type
                    != "TestableInterventionDetectionAgentOpenAI"
                ),
                listened_users=None,
                is_dm=is_dm,
            )

        if agent_type == DEFAULT_AUXILIARY:
            return cls(agent_type=DEFAULT_AUXILIARY, is_dm=is_dm)

        raise ValueError(
            f"Unknown auxiliary agent type {auxiliary_agent_type!r}",
        )
