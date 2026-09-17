"""
The agent that speaks to the users, on OpenAI. The only OpenAI-specific code
for replying and summarizing lives here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import suppress

from openai import AsyncOpenAI

from ...audio import AudioChunk
from ..main_agent import MainAgent, MainAgentEvent
from ..options import AgentCapabilities, MainAgentOptions

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_MODEL = "gpt-realtime"
SUMMARY_MODEL = "gpt-4o"

_MANAGEMENT_SCHEMA = {
    "name": "management_summary",
    "schema": {
        "type": "object",
        "properties": {
            "history_summary": {"type": "string"},
            "participant_roles_summary": {"type": "string"},
        },
        "required": [
            "history_summary",
            "participant_roles_summary",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}


class MainAgentOpenAI(MainAgent):
    """
    Speaks on OpenAI's Realtime API, and summarizes on Chat Completions.

    Realtime rebuilds its instructions before every response, since the
    cognition layer assembles a fresh full prompt each turn rather than
    holding conversational state itself. Summarization needs a JSON-schema
    response, which Realtime does not offer, so those two calls stay on
    Chat Completions.
    """

    def __init__(
        self,
        *,
        options: MainAgentOptions,
        client: AsyncOpenAI,
        model: str = DEFAULT_REALTIME_MODEL,
    ) -> None:
        super().__init__(options=options)

        self._client = client
        self._model = model

        self._connection_manager = None
        self._connection = None
        self._receive_task: asyncio.Task[None] | None = None

    async def _connect(self) -> None:
        self._connection_manager = self._client.realtime.connect(
            model=self._model,
        )
        self._connection = (
            await self._connection_manager.__aenter__()
        )

        session = await self._connection.session.update(
            session={
                "type": "realtime",
                "output_modalities": ["audio"],
            }
        )
        self.capabilities = self._read_capabilities(session)

        self._receive_task = asyncio.create_task(
            self._receive_loop(),
        )

    async def _disconnect(self) -> None:
        if self._receive_task is not None:
            self._receive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._connection_manager is not None:
            with suppress(Exception):
                await self._connection_manager.__aexit__(
                    None,
                    None,
                    None,
                )
            self._connection_manager = None
            self._connection = None

    async def _send_text(self, text: str, *, role: str) -> None:
        if self._connection is None:
            return

        await self._connection.conversation.item.create(
            item={
                "type": "message",
                "role": role if role in ("user", "system") else "user",
                "content": [
                    {"type": "input_text", "text": text},
                ],
            }
        )

    async def _send_audio_chunk(self, chunk: AudioChunk) -> None:
        if self._connection is None:
            return

        await self._connection.input_audio_buffer.append(
            audio=base64.b64encode(chunk.data).decode("ascii"),
        )

    async def _trigger_response(
        self,
        *,
        max_tokens: int | None,
        instructions: str,
        add_to_conversation: bool,
    ) -> None:
        if self._connection is None:
            return

        # The cognition layer rebuilds the full prompt every turn, so the
        # session's instructions are replaced rather than appended to.
        await self._connection.session.update(
            session={
                "type": "realtime",
                "instructions": instructions,
            }
        )
        await self._connection.response.create()
        # Marks a response as in flight so a later interrupt=True actually
        # has something to cancel instead of erroring on nothing.
        self._response_active = True

    async def _cancel_response(self) -> None:
        if self._connection is None:
            return

        await self._connection.response.cancel()

    async def _receive_loop(self) -> None:
        assert self._connection is not None

        async for event in self._connection:
            await self._handle_provider_message(
                event.model_dump_json(),
            )
            await self._interpret(event)

    async def _interpret(self, event) -> None:
        if event.type in ("response.done", "error"):
            # Both are terminal for this response -- clearing this here,
            # not just on success, is what keeps a later interrupt=True from
            # cancelling a response that already ended on its own.
            self._response_active = False

        match event.type:
            case "response.output_audio.delta":
                await self._stream_audio(
                    AudioChunk(
                        data=base64.b64decode(event.delta),
                    )
                )

            case (
                "response.output_text.delta"
                | "response.output_audio_transcript.delta"
            ):
                # The GA Realtime API names the spoken-word transcript event
                # differently for an audio-modality response than for a
                # text-modality one; this agent requests audio, so without
                # this branch assistant_text was always empty downstream.
                await self._stream_text(event.delta)

            case (
                "response.output_text.done"
                | "response.output_audio_transcript.done"
                | "response.done"
            ):
                await self._stream_text("", is_final=True)

            case "input_audio_buffer.speech_started":
                await self._raise_event(
                    MainAgentEvent.USER_SPEECH_STARTED,
                )

            case "input_audio_buffer.speech_stopped":
                await self._raise_event(
                    MainAgentEvent.USER_SPEECH_STOPPED,
                )

            case "response.output_audio.done":
                await self._raise_event(
                    MainAgentEvent.AGENT_SPEECH_STOPPED,
                )

            case "error":
                logger.warning(
                    "Main agent got a provider error: %s",
                    event.error,
                )

    def _read_capabilities(self, session) -> AgentCapabilities:
        turn_detection = getattr(
            getattr(session, "audio", None),
            "input",
            None,
        )
        turn_detection = getattr(
            turn_detection,
            "turn_detection",
            None,
        )

        if turn_detection is None:
            return AgentCapabilities()

        vad = getattr(turn_detection, "type", None) in (
            "server_vad",
            "semantic_vad",
        )
        interrupt = bool(
            getattr(turn_detection, "interrupt_response", False),
        )

        return AgentCapabilities(vad=vad, interrupt=interrupt)


class ChatCompletionsSummarizer:
    """
    The management-cycle summarizer, ported from MULCA's `MainLLM`.

    Satisfies cognition.ports.Summarizer structurally. Needs a JSON-schema
    response for the group cycle, which is not available on the Realtime
    API, so this stays on Chat Completions independent of which API the
    reply itself uses.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = SUMMARY_MODEL,
    ) -> None:
        self._client = client
        self._model = model

    async def summarize(
        self,
        *,
        transcript: str,
        previous_summary: str,
        previous_roles: str,
    ) -> tuple[str, str]:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are producing an updated internal summary of "
                        "an ongoing conversation, for another LLM to read "
                        "-- not a human. Optimize for information density: "
                        "concrete facts, names, numbers, and decisions, "
                        "not a list of topics that were merely mentioned. "
                        "Target roughly 250-300 words for history_summary."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Previous summary (empty if this is the first "
                        f"cycle):\n{previous_summary or '(none yet)'}\n\n"
                        "Previous participant/roles summary:\n"
                        f"{previous_roles or '(none yet)'}\n\n"
                        f"New messages since then:\n{transcript}\n\n"
                        "Produce an updated history_summary that "
                        "incorporates the new messages into the previous "
                        "summary (not just the new messages alone -- "
                        "earlier content that isn't repeated below must "
                        "still be retained), and an updated "
                        "participant_roles_summary."
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": _MANAGEMENT_SCHEMA,
            },
        )

        result = json.loads(response.choices[0].message.content)
        return (
            result["history_summary"],
            result["participant_roles_summary"],
        )

    async def summarize_private(
        self,
        *,
        transcript: str,
        previous_summary: str,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are producing an updated internal summary of "
                        "an ongoing private one-on-one conversation, for "
                        "another LLM to read -- not a human. Optimize for "
                        "information density: concrete facts, names, "
                        "numbers, and decisions, not a list of topics that "
                        "were merely mentioned. Target roughly 150-200 "
                        "words. Respond with only the updated summary "
                        "text, nothing else."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Previous summary (empty if this is the first "
                        f"cycle):\n{previous_summary or '(none yet)'}\n\n"
                        f"New messages since then:\n{transcript}\n\n"
                        "Produce an updated summary that incorporates the "
                        "new messages into the previous summary -- earlier "
                        "content that isn't repeated below must still be "
                        "retained."
                    ),
                },
            ],
        )

        return response.choices[0].message.content.strip()
