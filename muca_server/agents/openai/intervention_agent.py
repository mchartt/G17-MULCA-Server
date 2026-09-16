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

from ...cognition.ports import InterventionSignal
from ...cognition.strategies import StrategyStore
from ..auxiliary_agent import AuxiliaryAgent
from ..options import AuxiliaryAgentOptions

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-realtime"
TOOL_NAME = "signal_intervention"


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
    ) -> None:
        super().__init__(options=options)

        self._client = client
        self._strategies = strategies
        self._persona = persona
        self._task_knowledge = task_knowledge
        self._is_dm = is_dm
        self._model = model

        self._connection_manager = None
        self._connection = None
        self._receive_task: asyncio.Task[None] | None = None
        self._pending_signal: InterventionSignal | None = None

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
            }
        )

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
                "role": "user",
                "content": [
                    {"type": "input_text", "text": text},
                ],
            }
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

        await self._connection.response.create()

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
        """
        Turn a provider event into a decision.

        Does no strategy lookup, no cooldown check and no working-memory
        access -- it only reads what the provider said. Whether to act on it
        belongs to the cognition policy.
        """

        if event.type == "error":
            logger.warning(
                "Intervention agent %s got a provider error: %s",
                self.guid,
                event.error,
            )
            self._pending_signal = None
            await self._signal_decision(None)
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
            signal, self._pending_signal = (
                self._pending_signal,
                None,
            )
            await self._signal_decision(signal)

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
                },
                "required": [
                    "strategy_id",
                    "prompt_fragment",
                    "needs_history",
                    "needs_web_search",
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
            "current information from outside this conversation. Both "
            "default to false. If no strategy should fire, produce no "
            "output and do not call the tool. Never produce a "
            "conversational reply yourself; your only job is this "
            "judgment call.\n\n"
            "Available strategies:\n"
            f"{self._strategies.catalog_for_prompt()}"
        )
