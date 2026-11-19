"""
The agent that speaks to the users, on OpenAI. The only OpenAI-specific code
for replying and summarizing lives here.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from contextlib import suppress
from uuid import uuid4

from openai import AsyncOpenAI

from ...audio import AudioChunk
from ...audio.pcm import float32_to_pcm16, pcm16_to_float32, resample_float32
from ..main_agent import MainAgent, MainAgentEvent
from ..options import AgentCapabilities, MainAgentOptions
from ..voice_control_phrases import matches_dismissal
from ...usage_report import UsageTracker, usage_sink

logger = logging.getLogger(__name__)


# Below this many words, a substring match against the bias prompt is
# meaningless rather than suspicious -- found live: a bare "MULCA." or
# "assistant" is *always* a substring of the prompt (it contains those
# exact words), so without a floor this discarded genuine short wake-word
# attempts as if they were echoes, not just the real multi-sentence echo
# it was built to catch. Picked well above the 1-3 words a real address
# ("MULCA", "Hey MULCA", "assistant") normally is, and well below the
# ~12-word sentence the confirmed real echoes actually were.
MIN_PROMPT_ECHO_WORDS = 6


_WORD = re.compile(r"[\w']+")

# The wrapper the ASR model sometimes puts around an echoed prompt
# ("context: ###\n<prompt>\n###"), found live 19-25 Oct.
_ECHO_WRAPPER = re.compile(r"^\W*context\s*:|#+", re.IGNORECASE)


def _strip_prompt_echo(transcript: str, prompt: str) -> str:
    """
    `transcript` without the ASR model echoing its own vocabulary-bias
    prompt back -- a known failure mode (OpenAI's gpt-4o-transcribe/
    Whisper-family models) when there's little real speech signal: it
    hallucinates by repeating the prompt it was given instead of an
    empty/low-confidence result. Confirmed live: verbatim, with a sentence
    repeated, wrapped in "context: ###", and appended to real speech ("Ah,
    perché magari posso prendere dagli artefatti. The conversation is in
    English or Italian..."). The prompt says "MULCA start", so an echo
    left in woke MULCA up.

    Cheap, deterministic word matching -- no model call: every run of at
    least MIN_PROMPT_ECHO_WORDS words that also appears, in order, in the
    prompt is cut, along with the wrapper; the real speech around it is
    kept. "" when nothing real is left. The minimum keeps a short, genuine
    address that merely shares words with the prompt ("Hey MULCA").
    """

    prompt_words = [w.lower() for w in _WORD.findall(prompt)]
    spans = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(transcript)]

    cuts: list[tuple[int, int]] = []
    i = 0
    while i < len(spans):
        longest = 0
        for j, word in enumerate(prompt_words):
            if word != spans[i][2]:
                continue
            length = 0
            while (
                i + length < len(spans)
                and j + length < len(prompt_words)
                and spans[i + length][2] == prompt_words[j + length]
            ):
                length += 1
            longest = max(longest, length)
        if longest >= MIN_PROMPT_ECHO_WORDS:
            cuts.append((spans[i][0], spans[i + longest - 1][1]))
            i += longest
        else:
            i += 1

    if not cuts:
        return transcript

    kept, start = [], 0
    for cut_start, cut_end in cuts:
        kept.append(transcript[start:cut_start])
        start = cut_end
    kept.append(transcript[start:])
    # A piece with no words in it is punctuation the cut left dangling
    # ('", or "bot".' and the like).
    rest = " ".join(piece for piece in kept if _WORD.search(_ECHO_WRAPPER.sub("", piece)))
    rest = _ECHO_WRAPPER.sub(" ", rest)
    return re.sub(r"^[\W_]+", "", " ".join(rest.split()))


# "wake_v2": the transcriber's prompt wrapper left behind on its own, around
# real speech or nothing at all ("context:", "context: Hello.", "... You will
# receive additional context/instructions (separated by ### delimiters) from
# the user."). Found in the 26-28 Oct exports, after the echo itself was cut.
_PROMPT_WRAPPER_V2 = re.compile(
    r"you will receive additional context.*?from the user\.?|\bcontext\s*:|#+",
    re.IGNORECASE | re.DOTALL,
)


def _strip_prompt_wrapper(transcript: str) -> str:
    rest = _PROMPT_WRAPPER_V2.sub(" ", transcript)
    return re.sub(r"^[\W_]+", "", " ".join(rest.split()))


# A rough "don't trust this as real speech" cutoff on the ASR's own
# per-token confidence. Hallucinated transcripts (invented over
# near-silent/noisy audio -- confirmed live: coherent, fluent sentences
# ("I'm reading a book.") generated from pure kitchen noise, not just the
# bias-prompt echo _strip_prompt_echo already catches) are typically
# produced with much lower confidence than genuine transcription, even
# when fluent. A starting point, not calibrated against a real sample yet
# -- tune once there's confirmed-hallucination data to compare against
# confirmed-genuine (including quiet/accented) speech.
MIN_AVERAGE_LOGPROB = -1.5


def _average_logprob(logprobs) -> float | None:
    """None if there's nothing to average (not every provider response
    includes logprobs at all)."""

    if not logprobs:
        return None

    return sum(entry.logprob for entry in logprobs) / len(logprobs)


def _looks_like_low_confidence_transcription(logprobs) -> bool:
    """
    Whether the ASR's own per-token confidence for this transcript is low
    enough to doubt it's real speech, rather than something hallucinated.
    Unlike _strip_prompt_echo (one specific known string), this
    catches hallucinated content with no relation to any prompt text at
    all -- the general case, at the cost of needing a threshold rather
    than an exact match. None/empty logprobs (not every provider response
    includes them) means no signal either way, not a reason to discard.
    """

    average = _average_logprob(logprobs)
    return average is not None and average < MIN_AVERAGE_LOGPROB


DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1-mini"
SUMMARY_MODEL = "gpt-4o"
TRANSCRIPTION_MODEL = "gpt-4o-transcribe"
PROVIDER_SAMPLE_RATE = 24_000
MAX_OUTPUT_TOKENS = 4096

# Every response bills the whole session context again, so it is capped
# (input tokens per response, instructions included; see trim_context).
MAIN_TOKEN_LIMIT = 24_000

# Per 1M tokens: text in, cached text in, audio in, cached audio in, text
# out, audio out. The cost estimate behind the logs, the cost report and
# the `usage` events a client limits spending on (OpenAI pricing page,
# 2026-10-07).
PRICES_PER_MILLION = {
    "gpt-realtime": (4.0, 0.4, 32.0, 0.4, 16.0, 64.0),
    "gpt-realtime-2.1-mini": (0.6, 0.06, 10.0, 0.3, 2.4, 20.0),
    TRANSCRIPTION_MODEL: (2.5, 0.0, 2.5, 0.0, 10.0, 0.0),
    SUMMARY_MODEL: (2.5, 1.25, 0.0, 0.0, 10.0, 0.0),
    "gpt-5": (1.25, 0.125, 0.0, 0.0, 10.0, 0.0),
    # Group Coordinator on Azure (docs/coordinator/AZURE_SETUP.md): text
    # models chosen through MUCA_SUMMARY_MODEL / MUCA_COORDINATOR_MODEL.
    # OpenAI list prices, read 15 Nov 2026; Azure Global Standard prices
    # are assumed equal (NOT VERIFIED: check Cost Management).
    "gpt-6.1-sol": (2.0, 0.10, 0.0, 0.0, 10.0, 0.0),
    "gpt-6-luna": (0.10, 0.01, 0.0, 0.0, 0.50, 0.0),
    "text-embedding-3-small": (0.02, 0.0, 0.0, 0.0, 0.0, 0.0),
    # web_search.py's default model -- content tokens only; the tool's own
    # per-call fee is tracked separately (record_cost's extra_fee_usd),
    # not token-priced at all.
    "gpt-4o-mini": (0.15, 0.075, 0.0, 0.0, 0.60, 0.0),
}


async def trim_context(agent, response, token_limit: int) -> None:
    """
    Past `token_limit` input tokens, the session is full. The provider
    accepts truncation.token_limits but does not enforce it (checked on
    gpt-realtime-2.1-mini, 2026-10-07), so this does: it tells the
    conversation (on_context_full), which renews the memory and clears the
    session (docs/MEMORY_DESIGN.md). Without anyone to tell -- or if that
    renewal still hasn't come by 1.5x the limit -- it deletes the oldest
    half of the session's messages instead, as it always did.
    """

    usage = getattr(response, "usage", None)
    tokens = getattr(usage, "input_tokens", None) or 0
    if agent._connection is None or tokens <= token_limit:
        return

    if agent.on_context_full is not None and tokens <= token_limit * 1.5:
        if not agent._context_full_reported:
            agent._context_full_reported = True
            logger.info(
                "%s %s session full (%d input tokens > %d): renewing its memory",
                agent.log_label,
                type(agent).__name__,
                tokens,
                token_limit,
            )
            await agent.on_context_full()
        return

    drop = agent._item_ids[: len(agent._item_ids) // 2]
    del agent._item_ids[: len(drop)]
    for item_id in drop:
        await agent._connection.conversation.item.delete(item_id=item_id)
    logger.info(
        "%s %s trimmed its %d oldest messages (%d input tokens > %d)",
        agent.log_label,
        type(agent).__name__,
        len(drop),
        tokens,
        token_limit,
    )

# Errors that only mean a request raced the provider's own state (e.g. a
# cancel arriving just after the response finished): nothing to report.
# Testing only: end every provider connection after this many seconds, as
# OpenAI's 60-minute Realtime limit does, to exercise the recovery from it
# (see Conversation._handle_agent_connection_lost) without waiting an hour.
DEBUG_PROVIDER_TTL_ENV_VAR = "MUCA_DEBUG_PROVIDER_TTL_SECONDS"


async def clear_items(agent) -> None:
    """Delete every message in the agent's live session."""

    if agent._connection is None:
        return
    drop, agent._item_ids[:] = list(agent._item_ids), []
    for item_id in drop:
        with suppress(Exception):
            await agent._connection.conversation.item.delete(item_id=item_id)
    logger.info(
        "%s %s cleared its %d messages",
        agent.log_label,
        type(agent).__name__,
        len(drop),
    )


def debug_provider_ttl() -> float | None:
    value = os.environ.get(DEBUG_PROVIDER_TTL_ENV_VAR, "").strip()
    return float(value) if value else None


IGNORABLE_ERROR_CODES = frozenset(
    {
        "response_cancel_not_active",
    }
)


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
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        super().__init__(options=options)

        self._client = client
        self._persona = persona
        self._task_knowledge = task_knowledge
        self._model = model
        self._usage_tracker = usage_tracker
        self._v2 = options.listening_mode == "wake_v2"

        self._connection_manager = None
        self._connection = None
        self._receive_task: asyncio.Task[None] | None = None

        # The response in flight, and what of it has been produced so far.
        self._response_id: str | None = None
        self._response_modality = "audio"
        self._response_audio_started = False
        self._assistant_item_id: str | None = None
        self._assistant_audio_ms = 0

        # The last per-turn directive item, deleted before the next is sent.
        self._directive_item_id: str | None = None
        # The session's messages, oldest first, for trim_context.
        self._item_ids: list[str] = []
        # Set by a client's session.update: answer every turn in text.
        self._text_only = False

    def _build_instructions(self) -> str:
        return f"{self._persona}\n\nTask context:\n{self._task_knowledge}"

    def _transcription_prompt(self) -> str:
        # Factored out of _session_config so _strip_prompt_echo can
        # compare a completed transcript against this exact same string --
        # the one place this is worded.
        # "wake_v2" leaves the wake phrase out: an echoed prompt can't wake
        # MULCA up if it doesn't contain one.
        return (
            f"The conversation is in English or Italian, "
            f"sometimes switching between the two. One "
            f"participant is an AI assistant named "
            f"{self.name}, often addressed by that name, "
            f'"assistant", or "bot"'
            + ("." if self._v2 else f', and woken with "{self.name} start".')
        )

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
            # Otherwise a completed-transcription event's own logprobs is
            # always None, silently disabling
            # _looks_like_low_confidence_transcription entirely -- it has
            # nothing to check against.
            "include": ["item.input_audio_transcription.logprobs"],
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": PROVIDER_SAMPLE_RATE,
                    },
                    "transcription": {
                        "model": TRANSCRIPTION_MODEL,
                        # No "language" pin -- it only takes one ISO code,
                        # and this deployment's speakers switch between
                        # English and Italian, sometimes mid-conversation;
                        # forcing either one would hurt whichever isn't
                        # picked. "prompt" is non-exclusive vocabulary bias
                        # instead: both languages are expected, and the
                        # assistant's own name is worth transcribing
                        # correctly rather than as a phonetic near-miss.
                        "prompt": self._transcription_prompt(),
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": False,
                        "silence_duration_ms": 500,
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
        logger.info("%s main agent connected (%s)", self.log_label, self._model)
        if self._usage_tracker is not None:
            self._usage_tracker.record_connect(
                "main", self.instance_label or self.log_label
            )

        self._receive_task = asyncio.create_task(
            self._receive_loop(),
        )

    async def _clear_context(self) -> None:
        await clear_items(self)

    async def _disconnect(self) -> None:
        logger.info(
            "%s main agent disconnected, cost $%.4f",
            self.log_label,
            self.cost_usd,
        )
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

    async def _update_session(self, session: dict) -> None:
        # Only the instructions and the reply modality: the rest of the
        # session (turn detection, audio formats, transcription) is what the
        # server's turn taking relies on, and a client's own often uses
        # older field names.
        modalities_key = next(
            (k for k in ("output_modalities", "modalities") if k in session),
            None,
        )
        ignored = sorted(
            set(session) - {"instructions", "type", modalities_key}
        )
        if ignored:
            logger.info("%s ignoring session fields %s", self.log_label, ignored)

        # Without "audio" every reply is text, spoken turns included (e.g. a
        # client keeping its spending down); listening is unchanged.
        if modalities_key is not None:
            self._text_only = "audio" not in (session[modalities_key] or [])
            logger.info(
                "%s main agent replies in %s",
                self.log_label,
                "text only" if self._text_only else "the turn's own medium",
            )

        instructions = session.get("instructions")
        if not instructions or self._connection is None:
            return

        await self._connection.session.update(
            session={"type": "realtime", "instructions": instructions},
        )
        logger.info(
            "%s main agent instructions replaced by the client (%d chars)",
            self.log_label,
            len(instructions),
        )

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

        if self._text_only:
            output_modality = "text"

        # Pushed as an item, not folded into session.instructions -- a
        # session.update there would replace (not merge with) the
        # persona/task sent once at connect. This is CoalaPolicy
        # ._render_prompt's per-turn output (summary/recent-tail/directive
        # etc); empty when nothing new needs saying this turn. Only the
        # latest one is kept: left in, every directive (each with its own
        # recent transcript) would be billed again on every later response.
        if instructions:
            if self._directive_item_id is not None:
                await self._connection.conversation.item.delete(
                    item_id=self._directive_item_id,
                )
            self._directive_item_id = f"directive_{uuid4().hex[:20]}"
            await self._connection.conversation.item.create(
                item={
                    "id": self._directive_item_id,
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

        try:
            await self._connection.conversation.item.truncate(
                item_id=item_id,
                content_index=0,
                audio_end_ms=audio_end_ms,
            )
        except Exception:
            # Was a bare suppress(Exception) -- completely silent, so a
            # failure here (e.g. audio_end_ms past what the item actually
            # has, or the item already gone) was invisible. Logged now:
            # if this fires, the model's own memory keeps whatever it
            # thinks it said past the interruption point, which wouldn't
            # explain a live "won't stop talking", but could explain
            # degraded coherence on turns after a barge-in.
            logger.exception(
                "conversation.item.truncate() failed for item %s "
                "(audio_end_ms=%d) -- the model's memory of this reply may "
                "not reflect the interruption",
                item_id,
                audio_end_ms,
            )

    async def _cancel_response(self) -> None:
        if self._connection is None:
            return

        try:
            await self._connection.response.cancel()
        except Exception:
            # Not re-raised: this used to propagate straight out of
            # _interrupt_assistant_turn/_begin_assistant_turn (conversation.py),
            # skipping all of their cleanup -- main_agent_state stuck at
            # ANSWERING, _assistant_turn_ending stuck True forever, which
            # permanently blocks every future _end_assistant_turn() call via
            # its own guard. A real, observed failure mode: the response can
            # finish naturally (response.done) in the instant between
            # _response_active being read as True and this call reaching the
            # provider, and the API rejects cancelling a response that's
            # already done. Logged loudly -- if this fires a lot, that's the
            # "interrupting MULCA makes it stop responding" bug, confirmed.
            logger.exception(
                "response.cancel() failed -- the response may have already "
                "finished on its own; continuing anyway so the caller's own "
                "turn cleanup isn't skipped"
            )

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
            case "conversation.item.added":
                if event.item.type == "message":
                    self._item_ids.append(event.item.id)

            case "conversation.item.deleted":
                with suppress(ValueError):
                    self._item_ids.remove(event.item_id)

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
                if self._usage_tracker is not None:
                    self._usage_tracker.record_audio_ms(
                        "main",
                        self.instance_label or self.log_label,
                        chunk.duration_ms,
                    )

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
                usage = getattr(event, "usage", None)
                if getattr(usage, "type", None) == "tokens":
                    details = usage.input_token_details
                    record_cost(
                        "transcription",
                        TRANSCRIPTION_MODEL,
                        (
                            getattr(details, "text_tokens", 0) or 0,
                            0,
                            getattr(details, "audio_tokens", 0) or 0,
                            0,
                            usage.output_tokens or 0,
                            0,
                        ),
                        tracker=self._usage_tracker,
                    )
                transcript = event.transcript
                # Unconditional, regardless of outcome -- so the raw
                # per-transcript average can actually be reviewed against
                # real confirmed-hallucination/confirmed-genuine examples,
                # to judge whether MIN_AVERAGE_LOGPROB is a sane cutoff,
                # not just see the ones that already crossed it.
                average_logprob = _average_logprob(getattr(event, "logprobs", None))
                logger.debug(
                    "%s transcript %r has average logprob %s",
                    self.log_label,
                    transcript,
                    average_logprob,
                )
                stripped = _strip_prompt_echo(transcript, self._transcription_prompt())
                if self._v2:
                    stripped = _strip_prompt_wrapper(stripped)
                if stripped != transcript:
                    logger.warning(
                        "%s cut a prompt echo, not real speech, from a "
                        "transcript: %r -> %r",
                        self.log_label,
                        transcript,
                        stripped,
                    )
                    transcript = stripped
                elif (
                    average_logprob is not None
                    and average_logprob < MIN_AVERAGE_LOGPROB
                    and not matches_dismissal(transcript)
                ):
                    # -1.5 checked against a real live sample first (not
                    # just picked): across a full test session, the only
                    # confirmed-genuine utterance under this cutoff was a
                    # wake-phrase attempt ("assistente", a mis-rendered
                    # "assistant") -- every other sub-threshold transcript
                    # was confirmed hallucinated. That recurred live with
                    # dismissal attempts too ("stop"/"shut up" going
                    # unheard, an engaged session never closing) -- short,
                    # clipped control phrases are exactly what tends to
                    # score low confidence. Only a dismissal is exempted:
                    # honoring a hallucinated "stop" costs one "hey
                    # mulca", but honoring a hallucinated wake phrase
                    # (the ASR invents "assistant" boilerplate over
                    # near-silence) makes MULCA talk over the group --
                    # found live, the exact failure being fixed.
                    logger.warning(
                        "%s discarded a low-confidence transcript, likely "
                        "not real speech: %r",
                        self.log_label,
                        transcript,
                    )
                    transcript = ""

                await self._stream_transcript(
                    event.item_id,
                    transcript,
                )

            case "response.done":
                log_usage(self, self._model, event.response)
                await trim_context(self, event.response, MAIN_TOKEN_LIMIT)
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


def log_usage(agent, model: str, response) -> float:
    """
    Price one Realtime response's tokens (see record_cost) and add the cost
    to the agent's running total. Returns the cost.
    """

    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0

    def count(obj, name: str) -> int:
        return getattr(obj, name, None) or 0

    details = getattr(usage, "input_token_details", None)
    cached = getattr(details, "cached_tokens_details", None)
    output = getattr(usage, "output_token_details", None)
    tokens = (
        count(details, "text_tokens") - count(cached, "text_tokens"),
        count(cached, "text_tokens"),
        count(details, "audio_tokens") - count(cached, "audio_tokens"),
        count(cached, "audio_tokens"),
        count(output, "text_tokens"),
        count(output, "audio_tokens"),
    )
    kind = "main" if "Main" in type(agent).__name__ else "intervention"
    return record_cost(kind, model, tokens, agent=agent)


def record_cost(
    kind: str,
    model: str,
    tokens: tuple[int, int, int, int, int, int],
    *,
    agent=None,
    tracker: UsageTracker | None = None,
    extra_fee_usd: float = 0.0,
) -> float:
    """
    Price one OpenAI call -- tokens as (text in, cached text in, audio in,
    cached audio in, text out, audio out) -- log it, add it to the cost
    report, and send it to the session's client as a `usage` event. `kind`
    is what made the call: "main", "intervention", "transcription",
    "summary", "embedding", or "web_search". Returns the cost.

    extra_fee_usd: a flat, non-token-priced fee on top of the above (e.g.
    the web search tool's own per-call fee, separate from what its result
    content costs in tokens) -- 0 for every other kind.
    """

    prices = PRICES_PER_MILLION.get(model)
    cost = extra_fee_usd + (
        sum(n * price for n, price in zip(tokens, prices)) / 1_000_000
        if prices
        else 0.0
    )
    sink = usage_sink.get()

    if agent is not None:
        agent.cost_usd += cost
        label = agent.log_label
        instance = getattr(agent, "instance_label", "") or agent.log_label
        tracker = getattr(agent, "_usage_tracker", None)
        total = f", session ${agent.cost_usd:.4f}"
    else:
        label = instance = sink[0] if sink else ""
        total = ""

    logger.info(
        "%s %s usage (%s): in text=%d+%d cached, audio=%d+%d cached; "
        "out text=%d, audio=%d; cost $%.4f%s%s",
        label,
        type(agent).__name__ if agent is not None else kind,
        model,
        *tokens,
        cost,
        "" if prices else " (unknown model price)",
        total,
    )

    if tracker is not None:
        tracker.record_usage(kind, instance, tokens, cost)
        asyncio.create_task(tracker.maybe_alert())

    if sink is not None:
        asyncio.create_task(sink[1](kind, tokens, cost))

    return cost


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
