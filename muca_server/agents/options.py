from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

INTERVENTION_DETECTION = "intervention_detection"

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


class AuxiliaryAgentOptions(AgentOptions):
    agent_guid: UUID | None = None
    agent_type: str = "default"
    listen_to_assistant: bool = False

    # None means every user.
    listened_users: list[int] | None = None

    @classmethod
    def for_type(
        cls,
        auxiliary_agent_type: str,
    ) -> "AuxiliaryAgentOptions":
        """Build options for one of the agent types the client can ask for."""

        if auxiliary_agent_type == INTERVENTION_DETECTION:
            return cls(
                agent_type=INTERVENTION_DETECTION,
                listen_to_assistant=True,
                listened_users=None,
            )

        return cls(agent_type=auxiliary_agent_type)
