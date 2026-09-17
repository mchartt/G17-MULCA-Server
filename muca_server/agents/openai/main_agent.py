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

    Persona and task are sent once, at connect, the same way
    InterventionAgentOpenAI sends its own -- not "the stateless main LLM"
    originally specified. The old prototype used Chat Completions (genuinely
    stateless -- one HTTP call, full history resent as text every time).
    Realtime requires a persistent connection instead, needed for audio
    output; once that connection exists, it inherently accumulates every
    conversation item pushed to it, for as long as it stays open. Per-turn
    content (summary/recent-tail/directive, built by
    CoalaPolicy._render_prompt) is pushed as a plain conversation item in
    `_trigger_response` rather than folded into a rebuilt `instructions`
    string, since `session.update` replaces `instructions` rather than
    merging it -- overwriting it every turn would silently drop the
    persona/task sent at connect from every response after the first. This
    is why a restart matters here in a way "stateless" would not have
    implied: a freshly-opened session has no memory of anything, and only
    CoalaPolicy._render_prompt's verbatim-tail blocks (not this class) give
    it back what it lost.

    Summarization needs a JSON-schema response, which Realtime does not
    offer, so that stays on Chat Completions independent of the reply path.
    """

    def __init__(
        self,
        *,
        options: MainAgentOptions,
        client: AsyncOpenAI,
        persona: str = "",
        task_knowledge: str = "",
        model: str = DEFAULT_REALTIME_MODEL,
    ) -> None:
        super().__init__(options=options)

        self._client = client
        self._persona = persona
        self._task_knowledge = task_knowledge
        self._model = model

        self._connection_manager = None
        self._connection = None
        self._receive_task: asyncio.Task[None] | None = None

    def _build_instructions(self) -> str:
        return f"{self._persona}\n\nTask context:\n{self._task_knowledge}"

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
                "instructions": self._build_instructions(),
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

        # Pushed as an item, not folded into session.instructions -- a
        # session.update there would replace (not merge with) the
        # persona/task sent once at connect. This is CoalaPolicy
        # ._render_prompt's per-turn output (summary/recent-tail/directive
        # etc); empty when nothing new needs saying this turn.
        if instructions:
            await self._connection.conversation.item.create(
                item={
                    "type": "message",
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": instructions},
                    ],
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

            case "response.done":
                # response.output_text.done/response.output_audio_transcript
                # .done also fire, but response.done is the one signal
                # common to both modalities and guaranteed to fire exactly
                # once per response -- including both in this branch (as
                # this used to) double-fires is_final=True for an
                # audio-modality response (both events fire for the same
                # response), which raced CoalaPolicy's turn-completion
                # tracking: a stale second is_final for a finished turn
                # could resolve a *later* turn's still-pending record
                # instead, dropping its real reply from the episodic log.
                # Confirmed live: found via a two-turn scratch test where
                # the second turn's assistant_output logged as None despite
                # a real reply streaming to the client.
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
