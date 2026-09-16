from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from ..audio import AudioChunk
from .options import AgentCapabilities, MainAgentOptions
from .realtime_agent import RealtimeAgent

logger = logging.getLogger(__name__)


class MainAgentEvent(StrEnum):
    USER_SPEECH_STARTED = "user_speech_started"
    USER_SPEECH_STOPPED = "user_speech_stopped"
    AGENT_SPEECH_STARTED = "agent_speech_started"
    AGENT_SPEECH_STOPPED = "agent_speech_stopped"
    AGENT_SPEECH_INTERRUPTED = "agent_speech_interrupted"


class MainAgentState(StrEnum):
    INACTIVE = "inactive"
    ANSWERING = "answering"
    LISTENING = "listening"


EventHandler = Callable[[MainAgentEvent], Awaitable[None]]
AudioHandler = Callable[[AudioChunk], Awaitable[None]]
TextHandler = Callable[[str, bool], Awaitable[None]]


class MainAgent(RealtimeAgent):
    """
    The primary assistant in a conversation.

    It is the only agent that speaks to the users, so it is the only one that
    streams output audio and reports speech events. `capabilities` reflects
    what the connected provider actually does; a provider subclass sets it
    from the negotiated session config inside its own `_connect()` override.
    """

    def __init__(self, *, options: MainAgentOptions) -> None:
        super().__init__()

        self.options = options
        self.capabilities = AgentCapabilities()

        self.on_event: EventHandler | None = None
        self.on_audio: AudioHandler | None = None
        self.on_text: TextHandler | None = None

    @property
    def name(self) -> str:
        return self.options.name

    @property
    def agent_id(self) -> int:
        return self.options.agent_id

    async def connect(self) -> None:
        logger.info("Connecting MainAgent %s", self.name)

        await super().connect()

    async def _raise_event(
        self,
        event: MainAgentEvent,
    ) -> None:
        if self.on_event is not None:
            await self.on_event(event)

    async def _stream_audio(self, chunk: AudioChunk) -> None:
        if self.on_audio is not None:
            await self.on_audio(chunk)

    async def _stream_text(
        self,
        text: str,
        *,
        is_final: bool = False,
    ) -> None:
        if self.on_text is not None:
            await self.on_text(text, is_final)
