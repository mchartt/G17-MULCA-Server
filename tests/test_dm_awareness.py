"""
A DM's intervention agent must know it's in a DM.

Regression test for a bug where OpenAIAgentFactory baked is_dm into itself at
construction time -- but one factory instance is shared across every session
in the process, group and DM alike, so the intervention agent always thought
it was watching a group channel even inside a private, one-on-one DM where
every message is unambiguously addressed to it.
"""

import asyncio
from uuid import uuid4

from muca_server.agents import AuxiliaryAgent, Conversation, MainAgentOptions
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import SessionState


class RecordingAgentFactory:
    def __init__(self) -> None:
        self.built_with_is_dm: list[bool] = []

    def build_main(self, options):
        from muca_server.agents import MainAgent

        return MainAgent(options=options)

    def build_auxiliary(self, options) -> AuxiliaryAgent:
        self.built_with_is_dm.append(options.is_dm)
        return AuxiliaryAgent(options=options)


def make_conversation(factory: RecordingAgentFactory) -> Conversation:
    state = SessionState(session_id=uuid4())
    state.is_connected = True

    return Conversation(
        state=state,
        outbound_queue=asyncio.Queue(),
        main_agent_options=MainAgentOptions(),
        agent_factory=factory,
    )


async def test_a_dm_session_builds_its_intervention_agent_with_is_dm_true():
    factory = RecordingAgentFactory()
    conversation = make_conversation(factory)

    await conversation.connect_agent(
        parse_incoming_message(
            {"event": "connect", "conversationId": "dm:1", "isDm": True}
        )
    )
    await conversation.create_auxiliary_agent(
        parse_incoming_message(
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            }
        )
    )

    assert factory.built_with_is_dm == [True]


async def test_a_group_session_builds_its_intervention_agent_with_is_dm_false():
    factory = RecordingAgentFactory()
    conversation = make_conversation(factory)

    await conversation.connect_agent(
        parse_incoming_message(
            {"event": "connect", "conversationId": "group", "isDm": False}
        )
    )
    await conversation.create_auxiliary_agent(
        parse_incoming_message(
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            }
        )
    )

    assert factory.built_with_is_dm == [False]
