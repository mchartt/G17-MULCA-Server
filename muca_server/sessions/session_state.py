from dataclasses import dataclass, field
from uuid import UUID


@dataclass(slots=True)
class SessionState:
    session_id: UUID
    room_id: str | None = None

    is_connected: bool = False
    current_speaker: int = -1
    last_speaker: int = -1

    is_answering: bool = False
    assistant_spoke_last: bool = False

    playback_time_ms: int = 0

    active_response_id: str | None = None
    active_agent: str | None = None

    connected_agents: set[str] = field(
        default_factory=set
    )