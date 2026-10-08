"""
The observer that judges whether the assistant should speak, on OpenAI's
Realtime API. The only OpenAI-specific code for that judgement lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

from openai import AsyncOpenAI

from ...audio import AudioChunk
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
    log_usage,
    trim_context,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-realtime-2.1-mini"
TOOL_NAME = "signal_intervention"

# It responds on every turn, so its context is what is billed most often;
# it only needs the recent conversation to judge the latest message.
# Input tokens per judgement, instructions included; see trim_context.
INTERVENTION_TOKEN_LIMIT = 16_000


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
                "tools": [self._tool_definition()],
                "tool_choice": "auto",
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
            and event.name == TOOL_NAME
        ):
            self._pending_signal = self._parse_signal(
                event.arguments,
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
            needs_web_search=bool(
                args.get("needs_web_search", False),
            ),
            web_search_query=args.get("web_search_query", ""),
        )

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
                },
                "required": [
                    "strategy_id",
                    "prompt_fragment",
                    "needs_history",
                    "needs_web_search",
                    "web_search_query",
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

        return (
            f"{self._persona}\n\n"
            f"Task context:\n{self._task_knowledge}\n\n"
            f"{audience_note} For each new message, decide whether a "
            "conversational strategy should fire right now. If yes, call "
            f"the `{TOOL_NAME}` tool with the selected strategy id and a "
            "short, high-level instruction for the main response "
            "generator: the rationale and, if relevant, a rough target. "
            "Not literal wording -- the main response generator decides "
            "phrasing itself. Plus two independent flags: needs_history "
            "if answering well requires looking up something from earlier "
            "in the conversation; needs_web_search if answering requires "
            "current information from outside this conversation -- when "
            "true, also set web_search_query to the actual query to "
            "search for, in your own words, not the triggering message's "
            "raw phrasing. Both flags default to false. If no strategy "
            "should fire, produce no output and do not call the tool. "
            "Never produce a conversational reply yourself; your only "
            "job is this judgment call.\n\n"
            "Available strategies:\n"
            f"{self._strategies.catalog_for_prompt()}\n\n"
            "This session keeps growing for as long as the conversation "
            "lasts, so most of what you see below at any given moment is "
            "history, not something to react to now. Each time you're "
            "asked to decide, your judgment is about the most recent "
            "message and the most recent 'Current trigger variables' "
            "update specifically -- not something earlier in this "
            "conversation you may already have reacted to."
        )
