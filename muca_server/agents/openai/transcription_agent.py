"""
A hears-only agent for the "toggle"/"transcribe" listening modes (conversation.py):
transcribes audio continuously, with no reasoning or response-generation
capability of its own. Uses OpenAI's dedicated Realtime *transcription*
session type ("type": "transcription"), a distinct session kind from the
conversational one MainAgentOpenAI/InterventionAgentOpenAI use ("type":
"realtime") -- there is no `response.create()` on this connection at all,
so it cannot ever accidentally trigger a billed response; only the ASR
model's own (separate) transcription pricing applies, continuously.

NOT CURRENTLY WIRED IN. The "toggle"/"transcribe" modes' first cut
(conversation.py) reuses MainAgentOpenAI's existing transcription instead
(its VAD/turn
detection is what drives speaker-attribution today, and that machinery is
tightly coupled to the main agent's own audio -- moving it to a separate
hears-only connection is real, separate work, not just swapping which
agent gets send_audio_chunk). This class is built and tested (see
tests/test_transcription_agent_openai.py) ahead of that follow-up: the
future upgrade where the main agent stops hearing raw audio entirely (so
it, too, is free-while-idle, not just free-while-not-generating). If that
upgrade is never done, this file and its test are dead code and safe to
delete -- nothing else imports them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from openai import AsyncOpenAI

from ...audio import AudioChunk
from ..realtime_agent import RealtimeAgent
from .main_agent import (
    PROVIDER_SAMPLE_RATE,
    TRANSCRIPTION_MODEL,
    debug_provider_ttl,
    encode_for_provider,
    error_code,
    IGNORABLE_ERROR_CODES,
)

logger = logging.getLogger(__name__)

# (item_id, transcript)
TranscriptHandler = Callable[[str, str], Awaitable[None]]


class TranscriptionAgentOpenAI(RealtimeAgent):
    """
    Continuously transcribes a conversation's audio for the "toggle"/"transcribe" modes.

    Unlike MainAgentOpenAI (which also transcribes, as a side effect of a
    full conversational session), this agent has nothing to say and no
    session to bill for generation -- callers only ever read `on_transcript`.
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        model: str = TRANSCRIPTION_MODEL,
        prompt: str = "",
    ) -> None:
        super().__init__()

        self._client = client
        self._model = model
        self._prompt = prompt
        self._connection_manager = None
        self._connection = None
        self._receive_task: asyncio.Task[None] | None = None

        self.on_transcript: TranscriptHandler | None = None
        self.log_label = ""

    def _session_config(self) -> dict:
        return {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": PROVIDER_SAMPLE_RATE,
                    },
                    "transcription": {
                        "model": self._model,
                        "prompt": self._prompt,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 300,
                        "prefix_padding_ms": 500,
                    },
                },
            },
        }

    async def _connect(self) -> None:
        # No `model=` here: unlike MainAgentOpenAI/InterventionAgentOpenAI,
        # this session never generates a conversational response, so there
        # is no realtime model to pick -- the ASR model is session.audio
        # .input.transcription.model above, a separate concern.
        self._connection_manager = self._client.realtime.connect()
        self._connection = await self._connection_manager.__aenter__()

        await self._connection.session.update(
            session=self._session_config(),
        )
        logger.info(
            "%s transcription agent connected (%s)",
            self.log_label,
            self._model,
        )

        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _disconnect(self) -> None:
        if self._receive_task is not None:
            self._receive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._connection_manager is not None:
            with suppress(Exception):
                await self._connection_manager.__aexit__(None, None, None)
            self._connection_manager = None
            self._connection = None

    async def _send_audio_chunk(self, chunk: AudioChunk) -> None:
        if self._connection is None:
            return

        await self._connection.input_audio_buffer.append(
            audio=encode_for_provider(chunk),
        )

    async def _receive_loop(self) -> None:
        assert self._connection is not None

        reason = "the provider closed the connection"
        try:
            async with asyncio.timeout(debug_provider_ttl()):
                async for event in self._connection:
                    try:
                        await self._handle_provider_message(
                            event.model_dump_json(),
                        )
                        await self._interpret(event)
                    except Exception:
                        logger.exception(
                            "Could not handle a provider event of type %r",
                            getattr(event, "type", None),
                        )
        except TimeoutError:
            reason = "debug provider TTL reached"
        except Exception as exc:
            reason = repr(exc)

        await self._connection_lost(reason)

    async def _interpret(self, event) -> None:
        match event.type:
            case "conversation.item.input_audio_transcription.completed":
                if self.on_transcript is not None:
                    await self.on_transcript(event.item_id, event.transcript)

            case "conversation.item.input_audio_transcription.failed":
                logger.warning(
                    "%s transcription failed for item %s: %s",
                    self.log_label,
                    getattr(event, "item_id", None),
                    getattr(event, "error", None),
                )

            case "error":
                error = getattr(event, "error", None)
                if error_code(error) in IGNORABLE_ERROR_CODES:
                    logger.debug("Transcription agent: %s", error)
                    return

                logger.warning(
                    "Transcription agent got a provider error: %s",
                    error,
                )
