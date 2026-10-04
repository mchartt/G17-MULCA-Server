"""
The agent that speaks to the users, on OpenAI. The only OpenAI-specific code
for replying and summarizing lives here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import suppress

from openai import AsyncOpenAI

from ...audio import AudioChunk
from ...audio.pcm import float32_to_pcm16, pcm16_to_float32, resample_float32
from ..main_agent import MainAgent, MainAgentEvent
from ..options import AgentCapabilities, MainAgentOptions

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_MODEL = "gpt-realtime"
SUMMARY_MODEL = "gpt-4o"
TRANSCRIPTION_MODEL = "gpt-4o-transcribe"
PROVIDER_SAMPLE_RATE = 24_000
MAX_OUTPUT_TOKENS = 4096

# Errors that only mean a request raced the provider's own state (e.g. a
# cancel arriving just after the response finished): nothing to report.
# Testing only: end every provider connection after this many seconds, as
# OpenAI's 60-minute Realtime limit does, to exercise the recovery from it
# (see Conversation._handle_agent_connection_lost) without waiting an hour.
DEBUG_PROVIDER_TTL_ENV_VAR = "MUCA_DEBUG_PROVIDER_TTL_SECONDS"


def debug_provider_ttl() -> float | None:
    value = os.environ.get(DEBUG_PROVIDER_TTL_ENV_VAR, "").strip()
    return float(value) if value else None


IGNORABLE_ERROR_CODES = frozenset(
    {
        "response_cancel_not_active",
    }
)

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

        # The response in flight, and what of it has been produced so far.
        self._response_id: str | None = None
        self._response_modality = "audio"
        self._response_audio_started = False
        self._assistant_item_id: str | None = None
        self._assistant_audio_ms = 0

    def _build_instructions(self) -> str:
        return f"{self._persona}\n\nTask context:\n{self._task_knowledge}"

    def _session_config(self) -> dict:
        """
        Mirrors the Unity package's MainAgentOpenAiPayload: the provider
        detects and transcribes user speech, but never answers or stops on
        its own -- whether and when to speak is cognition's call, and
        barge-in is handled by the conversation (cancel + truncate).
        """

        return {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": self._build_instructions(),
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": PROVIDER_SAMPLE_RATE,
                    },
                    "transcription": {"model": TRANSCRIPTION_MODEL},
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": False,
                        "silence_duration_ms": 300,
                        "prefix_padding_ms": 500,
                    },
                },
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": PROVIDER_SAMPLE_RATE,
                    },
                },
            },
        }

    async def _connect(self) -> None:
        self._connection_manager = self._client.realtime.connect(
            model=self._model,
        )
        self._connection = (
            await self._connection_manager.__aenter__()
        )

        # session.update returns nothing (the provider answers later with a
        # session.updated event), so the capabilities come from the config
        # sent, not from reading a reply.
        await self._connection.session.update(
            session=self._session_config(),
        )
        self.capabilities = AgentCapabilities(vad=True, interrupt=False)

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
            audio=encode_for_provider(chunk),
        )

    async def _trigger_response(
        self,
        *,
        max_tokens: int | None,
        instructions: str,
        add_to_conversation: bool,
        output_modality: str | None = None,
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

        response = response_params(
            max_tokens=max_tokens,
            add_to_conversation=add_to_conversation,
            output_modality=output_modality,
        )
        if response:
            await self._connection.response.create(response=response)
        else:
            await self._connection.response.create()

        # Marks a response as in flight so a later interrupt=True actually
        # has something to cancel instead of erroring on nothing. The
        # provider's response.created confirms it (and covers responses
        # this agent did not ask for).
        self._response_active = True

    async def stop_speaking(self) -> None:
        # Stop generating, if still generating...
        await self.interrupt_response()

        # ...and, whether or not it was, cut the assistant's spoken item at
        # what the users actually heard: a reply is usually fully generated
        # long before it has finished playing, so an interruption mostly
        # lands after response.done. Otherwise the model believes it said
        # the part nobody heard. Needs the item id, not the response id (a
        # bug in the Unity original).
        if (
            self._connection is None
            or self._assistant_item_id is None
            or self._assistant_audio_ms <= 0
        ):
            return

        audio_end_ms = max(
            0,
            min(self.playback_time_ms, self._assistant_audio_ms),
        )
        item_id, self._assistant_item_id = self._assistant_item_id, None

        with suppress(Exception):
            await self._connection.conversation.item.truncate(
                item_id=item_id,
                content_index=0,
                audio_end_ms=audio_end_ms,
            )

    async def _cancel_response(self) -> None:
        if self._connection is None:
            return

        await self._connection.response.cancel()

    async def _receive_loop(self) -> None:
        assert self._connection is not None

        reason = "the provider closed the connection"
        try:
            # No timeout unless the testing-only TTL is set.
            async with asyncio.timeout(debug_provider_ttl()):
                async for event in self._connection:
                    # One event that fails to handle is a bug to log, not a
                    # reason to stop listening to every later one.
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
            reason = f"{DEBUG_PROVIDER_TTL_ENV_VAR} reached"
        except Exception as exc:
            reason = repr(exc)

        # Reached when the provider ends the connection (the SDK's iterator
        # just stops on a normal close, e.g. Realtime's 60-minute limit),
        # never on a disconnect(), which cancels this task instead.
        await self._connection_lost(reason)

    async def _interpret(self, event) -> None:
        if event.type == "response.done":
            # Terminal for this response -- clearing this here is what keeps
            # a later interrupt=True from cancelling a response that
            # already ended on its own.
            self._response_active = False

        match event.type:
            case "response.created":
                self._response_active = True
                self._response_id = getattr(event.response, "id", None)
                modalities = getattr(
                    event.response,
                    "output_modalities",
                    None,
                ) or ["audio"]
                self._response_modality = (
                    "audio" if "audio" in modalities else "text"
                )
                self._response_audio_started = False
                self._assistant_item_id = None
                self._assistant_audio_ms = 0

            case "response.output_item.added":
                if getattr(event.item, "role", None) == "assistant":
                    self._assistant_item_id = event.item.id

            case "response.output_audio.delta":
                chunk = AudioChunk(
                    data=pcm16_to_float32(base64.b64decode(event.delta)),
                    sample_rate=PROVIDER_SAMPLE_RATE,
                )
                self._assistant_audio_ms += chunk.duration_ms

                # The first audio of a response is when the assistant starts
                # to speak (MainAgentOpenAi.cs); the conversation then waits
                # for the client to actually play it before taking the turn.
                if not self._response_audio_started:
                    self._response_audio_started = True
                    await self._raise_event(
                        MainAgentEvent.AGENT_SPEECH_STARTED,
                    )

                await self._stream_audio(chunk)

            case "response.output_text.delta":
                await self._stream_text(event.delta, modality="text")

            case "response.output_audio_transcript.delta":
                # What is being said, for an audio-modality response.
                await self._stream_text(event.delta, modality="audio")

            case "conversation.item.input_audio_transcription.completed":
                await self._stream_transcript(
                    event.item_id,
                    event.transcript,
                )

            case "response.done":
                status = getattr(event.response, "status", None)
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
                await self._stream_text(
                    "",
                    is_final=True,
                    modality=self._response_modality,
                    interrupted=status in ("cancelled", "incomplete"),
                )

                # Generation is over; the turn itself only ends once the
                # client has finished playing it (the conversation waits).
                if self._response_audio_started:
                    await self._raise_event(
                        MainAgentEvent.AGENT_SPEECH_STOPPED,
                    )

                self._response_id = None
                self._response_audio_started = False

            case "input_audio_buffer.speech_started":
                self.last_input_item_id = getattr(event, "item_id", None)
                await self._raise_event(
                    MainAgentEvent.USER_SPEECH_STARTED,
                )

            case "input_audio_buffer.speech_stopped":
                await self._raise_event(
                    MainAgentEvent.USER_SPEECH_STOPPED,
                )

            case "error":
                error = getattr(event, "error", None)
                if error_code(error) in IGNORABLE_ERROR_CODES:
                    logger.debug("Main agent: %s", error)
                    return

                logger.warning(
                    "Main agent got a provider error: %s",
                    error,
                )


def error_code(error) -> str | None:
    if isinstance(error, dict):
        return error.get("code")

    return getattr(error, "code", None)


def encode_for_provider(chunk: AudioChunk) -> str:
    """Wire audio (float32, any rate) -> base64 PCM16 at the provider rate."""

    data = chunk.data
    if chunk.sample_rate != PROVIDER_SAMPLE_RATE:
        data = resample_float32(
            data,
            chunk.sample_rate,
            PROVIDER_SAMPLE_RATE,
        )

    return base64.b64encode(float32_to_pcm16(data)).decode("ascii")


def response_params(
    *,
    max_tokens: int | None,
    add_to_conversation: bool,
    output_modality: str | None,
) -> dict:
    """The response.create parameters that differ from the session's."""

    response: dict = {}

    if output_modality in ("text", "audio"):
        response["output_modalities"] = [output_modality]

    if max_tokens is not None:
        # The Unity client sends int.MaxValue for "no limit"
        # (RealtimeConnectionOpenAi.cs); the provider caps at 4096.
        response["max_output_tokens"] = (
            "inf"
            if max_tokens <= 0 or max_tokens > MAX_OUTPUT_TOKENS
            else max_tokens
        )

    if not add_to_conversation:
        response["conversation"] = "none"

    return response


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
                        "If something has to be compressed away to stay "
                        "within budget, prioritize in this order: (1) key "
                        "decisions and their outcomes, (2) important "
                        "process information -- who proposed or argued for "
                        "what, and why a decision was made the way it was, "
                        "not just what was decided, (3) the most important "
                        "task-specific details (e.g. agreed-on design "
                        "choices, numbers, constraints) that people "
                        "shouldn't have to repeat themselves on later. "
                        "Target roughly 1500 words for history_summary."
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
