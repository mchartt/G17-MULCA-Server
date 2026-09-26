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
# (text, is_final, modality, interrupted)
TextHandler = Callable[[str, bool, str, bool], Awaitable[None]]
# (input item id, transcript)
TranscriptHandler = Callable[[str, str], Awaitable[None]]


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

        # What the provider transcribed of the users' speech, per input
        # item: the only way spoken turns reach cognition as words.
        self.on_transcript: TranscriptHandler | None = None

        # The provider's id for the user speech that USER_SPEECH_STARTED
        # was raised for, when it has one; read by the conversation to tie
        # the later transcript to whoever was speaking then.
        self.last_input_item_id: str | None = None

        # How much of the current reply the client has played, in ms, as
        # the client last reported it; used to truncate on interruption.
        self.playback_time_ms = 0

    @property
    def name(self) -> str:
        return self.options.name

    @property
    def agent_id(self) -> int:
        return self.options.agent_id

    async def connect(self) -> None:
        logger.info("Connecting MainAgent %s", self.name)

        await super().connect()

    async def stop_speaking(self) -> None:
        """
        The users talked over the assistant: stop the reply, and make the
        provider forget whatever of it the users did not get to hear.

        Unlike interrupt_response, which only stops generation (and runs
        before every new response), this is for a real interruption.
        """

        await self.interrupt_response()

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
        modality: str = "text",
        interrupted: bool = False,
    ) -> None:
        if self.on_text is not None:
            await self.on_text(text, is_final, modality, interrupted)

    async def _stream_transcript(
        self,
        item_id: str,
        transcript: str,
    ) -> None:
        if self.on_transcript is not None:
            await self.on_transcript(item_id, transcript)
