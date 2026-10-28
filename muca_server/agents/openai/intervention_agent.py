"""
The observer that judges whether the assistant should speak, on OpenAI's
Realtime API. The only OpenAI-specific code for that judgement lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress

from openai import AsyncOpenAI

from ...audio import AudioChunk
from ...cognition.memory.day_memory import HISTORY_PERIODS
from ...cognition.ports import InterventionSignal
from ...cognition.strategies import StrategyStore
from ..auxiliary_agent import AuxiliaryAgent
from ..options import AuxiliaryAgentOptions
from ...usage_report import UsageTracker
from .main_agent import (
    DEBUG_PROVIDER_TTL_ENV_VAR,
    IGNORABLE_ERROR_CODES,
    PROVIDER_SAMPLE_RATE,
    debug_provider_ttl,
    encode_for_provider,
    error_code,
    clear_items,
    log_usage,
    trim_context,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-realtime-2.1-mini"
TOOL_NAME = "signal_intervention"

# "wake_v2": silence is a tool call too, and every judgement must make one --
# with tool_choice "auto" and only signal_intervention, staying silent meant
# producing nothing, and the 15-28 Oct exports show 634 replies on turns
# that met none of respond_when_addressed's conditions. The reason it gives
# goes into the turn's log. MUCA_WAKE_V2_FORCED_TOOL=0 turns this off.
SILENCE_TOOL_NAME = "no_intervention"
SILENCE_REASONS = (
    "not_addressed",
    "about_mulca",
    "reaction_or_fragment",
    "teammate_talk",
    "unclear_transcript",
    "other",
)
FORCED_TOOL_V2 = os.environ.get("MUCA_WAKE_V2_FORCED_TOOL", "1") != "0"

# The same as the main agent's (docs/MEMORY_DESIGN.md): it decides whether
# MULCA speaks, so it shouldn't know less than the agent that speaks. It
# does respond on every judged turn, but mostly from cache, where the extra
# context costs ~$0.0005 per turn. Input tokens per judgement, instructions
# included; see trim_context.
INTERVENTION_TOKEN_LIMIT = 24_000


HISTORY_NOTE = (
    "This session keeps growing for as long as the conversation "
    "lasts, so most of what you see below at any given moment is "
    "history, not something to react to now. Each time you're "
    "asked to decide, your judgment is about the most recent "
    "message and the most recent 'Current trigger variables' "
    "update specifically -- not something earlier in this "
    "conversation you may already have reacted to."
)


class InterventionAgentOpenAI(AuxiliaryAgent):
    """
    Judges, on each turn, whether a conversational strategy should fire.

    A persistent Realtime session: the persona, task and strategy catalog are
    sent once at connect time, and the session accumulates its own memory of
    what it is shown afterwards.
    """

    def __init__(
        self,
        *,
        options: AuxiliaryAgentOptions,
        client: AsyncOpenAI,
        strategies: StrategyStore,
        persona: str,
        task_knowledge: str,
        is_dm: bool = False,
        model: str = DEFAULT_MODEL,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        super().__init__(options=options)

        self._client = client
        self._strategies = strategies
        self._persona = persona
        self._task_knowledge = task_knowledge
        self._is_dm = is_dm
        self._model = model
        self._usage_tracker = usage_tracker
        self._v2 = options.listening_mode == "wake_v2"
        self._forced_tool = self._v2 and FORCED_TOOL_V2

        self._connection_manager = None
        self._connection = None
        self._receive_task: asyncio.Task[None] | None = None
        self._pending_signal: InterventionSignal | None = None
        # A turn arrived while a judgement was running: judge once more
        # when it ends, rather than cancelling it.
        self._rejudge = False
        # The session's messages, oldest first, for trim_context.
        self._item_ids: list[str] = []

    async def _connect(self) -> None:
        self._connection_manager = self._client.realtime.connect(
            model=self._model,
        )
        self._connection = (
            await self._connection_manager.__aenter__()
        )

        await self._connection.session.update(
            session={
                "type": "realtime",
                "output_modalities": ["text"],
                "instructions": self._build_instructions(),
                "tools": (
                    [self._silence_tool_definition(), self._tool_definition()]
                    if self._forced_tool
                    else [self._tool_definition()]
                ),
                "tool_choice": "required" if self._forced_tool else "auto",
                # It hears the conversation's audio too (the Unity
                # original's intervention agent does), and VAD commits that
                # audio into its context -- but it only ever judges when the
                # conversation asks it to, never on its own.
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": PROVIDER_SAMPLE_RATE,
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "create_response": False,
                            "interrupt_response": False,
                        },
                    },
                },
            }
        )
        logger.info(
            "%s intervention agent connected (%s)",
            self.log_label,
            self._model,
        )
        if self._usage_tracker is not None:
            self._usage_tracker.record_connect(
                "intervention", self.instance_label or self.log_label
            )

        self._receive_task = asyncio.create_task(
            self._receive_loop(),
        )

    async def _clear_context(self) -> None:
        await clear_items(self)

    async def _disconnect(self) -> None:
        logger.info(
            "%s intervention agent disconnected, cost $%.4f",
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

        # Set before the await, so a turn arriving meanwhile is coalesced
        # (trigger_response below) instead of creating a second response.
        self._response_active = True
        await self._connection.response.create()

    async def trigger_response(self, **kwargs) -> None:
        # Never cancels: a cancelled judgement is still billed in full,
        # uncached, and its decision is lost. The next judgement sees every
        # turn that arrived meanwhile, so each turn is still judged.
        if self._response_active:
            self._rejudge = True
            return

        await super().trigger_response(**kwargs)

    async def _judge_again_if_asked(self) -> None:
        if self._rejudge:
            self._rejudge = False
            await self._trigger_response(
                max_tokens=None,
                instructions="",
                add_to_conversation=True,
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
        """
        Turn a provider event into a decision.

        Does no strategy lookup, no cooldown check and no working-memory
        access -- it only reads what the provider said. Whether to act on it
        belongs to the cognition policy.
        """

        if event.type == "response.created":
            self._response_active = True
            return

        if event.type == "conversation.item.added":
            if event.item.type == "message":
                self._item_ids.append(event.item.id)
            return

        if event.type == "conversation.item.deleted":
            with suppress(ValueError):
                self._item_ids.remove(event.item_id)
            return

        if event.type == "error":
            if error_code(event.error) in IGNORABLE_ERROR_CODES:
                return

            logger.warning(
                "Intervention agent %s got a provider error: %s",
                self.guid,
                event.error,
            )

            # An error unrelated to a judgement in flight (a rejected item,
            # say) must not throw away a decision that is still coming, nor
            # report a decision nobody asked for.
            if not self._response_active:
                return

            # Terminal for this judgement, same as response.done below.
            self._response_active = False
            self._pending_signal = None
            await self._signal_decision(None)
            await self._judge_again_if_asked()
            return

        if (
            event.type == "response.function_call_arguments.done"
            and event.name in (TOOL_NAME, SILENCE_TOOL_NAME)
        ):
            self._pending_signal = (
                self._parse_signal(event.arguments)
                if event.name == TOOL_NAME
                else self._parse_silence(event.arguments)
            )

            # The session is never told the call resolved, MULCA never
            # answered it, and the call accumulates in session state.
            if self._connection is not None:
                await self._connection.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": event.call_id,
                        "output": "acknowledged",
                    }
                )
            return

        if event.type == "response.done":
            log_usage(self, self._model, event.response)
            await trim_context(self, event.response, INTERVENTION_TOKEN_LIMIT)
            self._response_active = False
            signal, self._pending_signal = (
                self._pending_signal,
                None,
            )
            await self._signal_decision(signal)
            await self._judge_again_if_asked()

    def _parse_signal(
        self,
        raw_arguments: str,
    ) -> InterventionSignal | None:
        try:
            args = json.loads(raw_arguments)
        except json.JSONDecodeError:
            logger.warning(
                "Could not parse %s arguments: %r",
                TOOL_NAME,
                raw_arguments,
            )
            return None

        strategy_id = args.get("strategy_id")
        if not strategy_id:
            return None

        return InterventionSignal(
            strategy_id=strategy_id,
            prompt_fragment=args.get("prompt_fragment", ""),
            needs_history=bool(args.get("needs_history", False)),
            history_period=str(args.get("history_period") or "all"),
            needs_web_search=bool(
                args.get("needs_web_search", False),
            ),
            web_search_query=args.get("web_search_query", ""),
            flag_offensive_language=bool(
                args.get("flag_offensive_language", False),
            ),
            nudge_participation=bool(
                args.get("nudge_participation", False),
            ),
        )

    def _parse_silence(self, raw_arguments: str) -> InterventionSignal:
        """A chosen silence: no strategy, and why, for the turn's log."""

        try:
            args = json.loads(raw_arguments)
        except json.JSONDecodeError:
            args = {}
        reason = f"{SILENCE_TOOL_NAME}: {args.get('reason') or 'other'}"
        if args.get("note"):
            reason += f" -- {args['note']}"
        logger.info("%s judge chose silence (%s)", self.log_label, reason)
        return InterventionSignal(
            strategy_id="",
            prompt_fragment="",
            silence_reason=reason,
        )

    def _silence_tool_definition(self) -> dict:
        return {
            "type": "function",
            "name": SILENCE_TOOL_NAME,
            "description": (
                "The usual answer: MULCA stays silent on this turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": list(SILENCE_REASONS),
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional, at most 12 words.",
                    },
                },
                "required": ["reason"],
            },
        }

    def _tool_definition(self) -> dict:
        return {
            "type": "function",
            "name": TOOL_NAME,
            "description": (
                "Signal that a conversational strategy should fire now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {
                        "type": "string",
                        "enum": self._strategies.ids(),
                    },
                    "prompt_fragment": {
                        "type": "string",
                        "description": (
                            "High-level rationale/target only -- why this "
                            "strategy applies now, and a rough target if "
                            "relevant. Not literal wording or a script."
                        ),
                    },
                    "needs_history": {"type": "boolean"},
                    "history_period": {
                        "type": "string",
                        "description": (
                            "Only when needs_history is true or "
                            "strategy_id is provide_summary: the period "
                            "the question is about -- one of "
                            + ", ".join(HISTORY_PERIODS)
                            + ", or a date the message names, as "
                            "YYYY-MM-DD. Default all."
                        ),
                    },
                    "needs_web_search": {"type": "boolean"},
                    "web_search_query": {
                        "type": "string",
                        "description": (
                            "Only when needs_web_search is true: the "
                            "actual query to search for, in your own "
                            "words -- not the triggering message's raw "
                            "phrasing. Empty otherwise."
                        ),
                    },
                    "flag_offensive_language": {
                        "type": "boolean",
                        "description": (
                            "True if the triggering message also contains "
                            "offensive or disrespectful language, AND "
                            "strategy_id is something other than "
                            "pleasant_conversation -- a brief aside gets "
                            "spliced onto that reply. If nothing else is "
                            "firing, select pleasant_conversation as "
                            "strategy_id instead of using this flag."
                        ),
                    },
                    "nudge_participation": {
                        "type": "boolean",
                        "description": (
                            "True if a quiet participant is worth a brief "
                            "invitation alongside a different primary "
                            "reply, AND strategy_id is something other "
                            "than participation_equality_text. If nothing "
                            "else is firing, select "
                            "participation_equality_text as strategy_id "
                            "instead of using this flag."
                        ),
                    },
                },
                "required": [
                    "strategy_id",
                    "prompt_fragment",
                    "needs_history",
                    "needs_web_search",
                    "web_search_query",
                    "flag_offensive_language",
                    "nudge_participation",
                ],
            },
        }

    def _build_instructions(self) -> str:
        audience_note = (
            "You are in a private, one-on-one conversation with a single "
            "user -- there is no other audience, so unlike a group "
            "channel, there's no ambiguity about who a message is for. "
            "This does not mean every message needs a reply: still judge "
            "whether a response is actually warranted right now."
            if self._is_dm
            else "You are watching an ongoing multi-user conversation, "
            "one message at a time, as it happens."
        )

        if self._v2:
            # "wake_v2": the decision first, the brief and the flags after.
            decision = (
                f"{audience_note} For each new message, call exactly one "
                f"tool: `{SILENCE_TOOL_NAME}` (the usual answer: MULCA stays "
                f"silent) with your reason, or `{TOOL_NAME}` with the "
                "strategy that should fire. "
                if self._forced_tool
                else f"{audience_note} For each new message, either call "
                f"`{TOOL_NAME}` with the strategy that should fire, or call "
                "no tool at all (the usual answer: MULCA stays silent). "
            )
            return (
                f"{self._persona}\n\n"
                f"Task context:\n{self._task_knowledge}\n\n"
                f"{decision}With `{TOOL_NAME}`, prompt_fragment is a short, "
                "high-level instruction for the main response generator: "
                "the rationale and, if relevant, a rough target -- what the "
                "reply should do, not literal wording. A brief never "
                "mentions MULCA's capabilities or limitations and never asks "
                "people to paste or share something, unless the speaker "
                "asked for exactly that. "
                f"{self._flags_instructions()} Never produce a "
                "conversational reply yourself; your only job is this "
                "judgment call.\n\n"
                "Available strategies:\n"
                f"{self._strategies.catalog_for_prompt()}\n\n"
                "This session keeps growing for as long as the conversation "
                "lasts, so most of what you see is history, not something to "
                "react to now: each decision is about the most recent message "
                "and its turn facts only."
            )

        return (
            f"{self._persona}\n\n"
            f"Task context:\n{self._task_knowledge}\n\n"
            f"{audience_note} For each new message, decide whether a "
            "conversational strategy should fire right now. If yes, call "
            f"the `{TOOL_NAME}` tool with the selected strategy id and a "
            "short, high-level instruction for the main response "
            "generator: the rationale and, if relevant, a rough target. "
            "Not literal wording -- the main response generator decides "
            "phrasing itself. Describe what the reply should do, never "
            "what the response generator can or can't see: it has the "
            "course material and schedule in your task context, the whole "
            "conversation (long pasted text and earlier messages "
            "included), and the text of attached text, Word, PDF and "
            "PowerPoint files. So never brief it to say it can't read a "
            "file, see the schedule or find earlier messages, or to ask "
            "for something to be pasted again -- tell it what to do with "
            "them. For a request about an attached file, say "
            "what to do with the file; never describe or limit what it "
            "contains -- you only get a note of it, the response generator "
            f"reads it. {self._flags_instructions()} If "
            "no strategy should fire, produce no output and do not call "
            "the tool. "
            "Never produce a conversational reply yourself; your only "
            "job is this judgment call.\n\n"
            "Available strategies:\n"
            f"{self._strategies.catalog_for_prompt()}\n\n"
            f"{HISTORY_NOTE}"
        )

    def _flags_instructions(self) -> str:
        return (
            "Plus two independent flags: needs_history "
            "if answering well requires looking up something from earlier "
            "in the conversation -- with it, and whenever you select "
            "provide_summary, set history_period to the period the "
            "question is about (\"today\", \"yesterday\", \"last "
            "session\"...), or all; needs_web_search if answering requires "
            "current information from outside this conversation -- when "
            "true, also set web_search_query to the actual query to "
            "search for, in your own words, not the triggering message's "
            "raw phrasing. Setting needs_web_search triggers a real search "
            "whose results the response generator will actually have -- "
            "do not write a prompt_fragment that tells it to hedge, caveat "
            "its browsing ability, or treat the answer as unverified; if "
            "you're not confident a search will find a good answer, don't "
            "set the flag at all rather than ask for a hedged one. Two "
            "more independent flags: flag_offensive_language and "
            "nudge_participation, each a brief aside spliced onto whatever "
            "strategy_id's reply already is, for when that concern applies "
            "alongside a different primary strategy rather than being the "
            "reason for responding at all (e.g. a direct question that "
            "also contained offensive language: strategy_id= "
            "respond_when_addressed, flag_offensive_language=true). If "
            "nothing else would fire, select that strategy's own id as "
            "strategy_id instead of setting its flag -- these flags are "
            "only for riding along, never a replacement for the normal "
            "standalone firing of pleasant_conversation or "
            "participation_equality_text. All flags default to false."
        )
