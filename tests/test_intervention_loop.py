"""
The full loop: an utterance asks an observer to judge, and its judgement
reaches the main agent through cognition -- with no OpenAI code involved.

This is what proves Conversation.trigger_response() (the request half) and
AuxiliaryAgent.on_decision (the response half) actually compose with
CoalaPolicy the way the design intends.
"""

import asyncio
from uuid import uuid4

from muca_server.agents import AuxiliaryAgent, MainAgent, MainAgentOptions
from muca_server.agents.conversation import Conversation
from muca_server.cognition import CoalaPolicy, InterventionSignal
from muca_server.cognition.strategies import Strategy, StrategyStore
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import SessionState

STRATEGIES = StrategyStore(
    {
        "respond_when_addressed": Strategy(
            id="respond_when_addressed",
            trigger_description="addressed directly",
            prompt_template="Answer them. {prompt_fragment}",
        ),
    }
)


class FakeInterventionAgent(AuxiliaryAgent):
    """An observer that always signals the same strategy."""

    signal: InterventionSignal | None = InterventionSignal(
        strategy_id="respond_when_addressed",
        prompt_fragment="they asked a direct question",
    )

    async def _trigger_response(
        self,
        *,
        max_tokens,
        instructions,
        add_to_conversation,
        **_,
    ) -> None:
        await self._signal_decision(self.signal)


class ScriptedMainAgent(MainAgent):
    def __init__(self, *, options: MainAgentOptions) -> None:
        super().__init__(options=options)
        self.instructions: list[str] = []

    async def _trigger_response(
        self,
        *,
        max_tokens,
        instructions,
        add_to_conversation,
        **_,
    ) -> None:
        self.instructions.append(instructions)
        await self._stream_text("Sure, ")
        await self._stream_text("happy to help.", is_final=True)


class FixedAgentFactory:
    def __init__(self) -> None:
        self.main: ScriptedMainAgent | None = None
        self.auxiliary: FakeInterventionAgent | None = None

    def build_main(self, options):
        self.main = ScriptedMainAgent(options=options)
        return self.main

    def build_auxiliary(self, options):
        self.auxiliary = FakeInterventionAgent(options=options)
        return self.auxiliary


def create_auxiliary():
    return parse_incoming_message(
        {
            "event": "create_auxiliary_agent",
            "auxiliaryAgentType": "intervention_detection",
        }
    )


async def drain(outbound: asyncio.Queue) -> list:
    messages = []
    while not outbound.empty():
        messages.append(outbound.get_nowait())
    return messages


async def test_an_observers_signal_reaches_the_main_agent():
    state = SessionState(session_id=uuid4())
    outbound: asyncio.Queue = asyncio.Queue()
    factory = FixedAgentFactory()
    policy = CoalaPolicy(
        strategy_store=STRATEGIES,
    )

    conversation = Conversation(
        state=state,
        outbound_queue=outbound,
        main_agent_options=MainAgentOptions(),
        agent_factory=factory,
        policy=policy,
    )

    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    await conversation.route_user_text(
        parse_incoming_message(
            {
                "event": "user_text",
                "speaker": 1,
                "speakerName": "Ada",
                "text": "@MULCA are you there?",
            }
        )
    )

    await conversation.wait_until_idle()

    # trigger_response() is fired as a background task.
    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.main.instructions
    assert "Answer them." in factory.main.instructions[0]

    texts = [
        message.text
        for message in await drain(outbound)
        if message.event == "assistant_text"
    ]
    assert "".join(texts) == "Sure, happy to help."


async def test_the_observer_declining_produces_no_reply():
    state = SessionState(session_id=uuid4())
    outbound: asyncio.Queue = asyncio.Queue()
    factory = FixedAgentFactory()
    policy = CoalaPolicy(
        strategy_store=STRATEGIES,
    )

    conversation = Conversation(
        state=state,
        outbound_queue=outbound,
        main_agent_options=MainAgentOptions(),
        agent_factory=factory,
        policy=policy,
    )

    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())
    factory.auxiliary.signal = None

    await conversation.route_user_text(
        parse_incoming_message(
            {
                "event": "user_text",
                "speaker": 1,
                "text": "just chatting",
            }
        )
    )

    await conversation.wait_until_idle()

    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.main.instructions == []
