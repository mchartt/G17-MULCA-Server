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
    ) -> "StateUpdateMessage":
        return cls(
            is_connected=True,
            current_speaker=current_speaker,
            last_speaker=last_speaker,
            is_answering=True,
            assistant_spoke_last=True,
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

    agent_guid: str = Field(serialization_alias="agentGuid")
    message: str
    
class AgentGuidMessage(OutgoingMessage):
    event: Literal["agent_guid"] = "agent_guid"

    agent_guid: str = Field(serialization_alias="agentGuid")
    

OutgoingProtocolMessage = (
    StateUpdateMessage
    | AgentMessage
    | AgentGuidMessage
)

def serialize_outgoing_message(
    message: OutgoingProtocolMessage,
) -> str:
    return message.model_dump_json(
        by_alias=True,
        exclude_none=True,
    )