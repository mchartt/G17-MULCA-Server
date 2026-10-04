"""
A provider connection that ends on its own -- OpenAI closes every Realtime
session after 60 minutes -- ends the whole session, after saving its memory,
so the client reopens a fresh one. A disconnect() asked for here never does.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from muca_server import create_app
from muca_server.agents import MainAgent
from muca_server.agents.openai.intervention_agent import InterventionAgentOpenAI
from muca_server.agents.openai.main_agent import (
    DEBUG_PROVIDER_TTL_ENV_VAR,
    MainAgentOpenAI,
)
from muca_server.agents.options import AuxiliaryAgentOptions, MainAgentOptions
from muca_server.cognition.strategies import StrategyStore
from muca_server.sessions import DefaultSessionComponentsFactory


def make_main_agent() -> MainAgentOpenAI:
    return MainAgentOpenAI(options=MainAgentOptions(), client=object())


def make_intervention_agent() -> InterventionAgentOpenAI:
    return InterventionAgentOpenAI(
        options=AuxiliaryAgentOptions(),
        client=object(),
        strategies=StrategyStore({}),
        persona="p",
        task_knowledge="t",
    )


AGENTS = pytest.mark.parametrize(
    "make_agent",
    [make_main_agent, make_intervention_agent],
    ids=["main", "intervention"],
)


class _Event:
    def __init__(self, type: str) -> None:
        self.type = type

    def model_dump_json(self) -> str:
        return f'{{"type": "{self.type}"}}'


class EndingConnection:
    """Yields these events, then ends -- as the SDK does on a normal close."""

    def __init__(self, *events, raises: Exception | None = None) -> None:
        self._events = list(events)
        self._raises = raises

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event
        if self._raises is not None:
            raise self._raises


class EndlessConnection:
    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover


def connected(agent, connection):
    agent._connection = connection
    agent._connected = True
    lost: list[object] = []

    async def record(lost_agent) -> None:
        lost.append(lost_agent)

    agent.on_connection_lost = record
    return lost


@AGENTS
async def test_a_connection_the_provider_ends_is_reported_once(make_agent):
    agent = make_agent()
    lost = connected(agent, EndingConnection(_Event("session.updated")))

    await agent._receive_loop()

    assert lost == [agent]
    assert not agent.is_connected


@AGENTS
async def test_a_connection_that_fails_is_reported_too(make_agent):
    agent = make_agent()
    lost = connected(agent, EndingConnection(raises=ConnectionError("gone")))

    await agent._receive_loop()

    assert lost == [agent]


@AGENTS
async def test_a_disconnect_asked_for_is_not_a_lost_connection(make_agent):
    agent = make_agent()
    lost = connected(agent, EndlessConnection())
    agent._receive_task = asyncio.create_task(agent._receive_loop())
    await asyncio.sleep(0)

    await agent.disconnect()

    assert lost == []


@AGENTS
async def test_one_event_that_fails_to_handle_does_not_end_the_connection(
    make_agent,
):
    agent = make_agent()
    handled: list[str] = []

    async def on_message(raw: str) -> None:
        handled.append(raw)
        if len(handled) == 1:
            raise ValueError("a bug in one handler")

    lost = connected(
        agent,
        EndingConnection(_Event("first"), _Event("second")),
    )
    agent.on_message = on_message

    await agent._receive_loop()

    assert len(handled) == 2
    assert lost == [agent]


@AGENTS
async def test_the_testing_ttl_ends_a_connection_early(make_agent, monkeypatch):
    monkeypatch.setenv(DEBUG_PROVIDER_TTL_ENV_VAR, "0.05")
    agent = make_agent()
    lost = connected(agent, EndlessConnection())

    await asyncio.wait_for(agent._receive_loop(), timeout=2)

    assert lost == [agent]


# ----------------------------------------------------------------------
# The whole session
# ----------------------------------------------------------------------


class DroppingMainAgent(MainAgent):
    """Loses its provider connection when asked for its first response."""

    async def _trigger_response(self, **kwargs) -> None:
        await self._connection_lost("the provider ended the session")


class Factory:
    def build_main(self, options):
        return DroppingMainAgent(options=options)

    def build_auxiliary(self, options):  # pragma: no cover
        raise AssertionError("not used")


class CountingPolicy:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def start(self, port) -> None:
        self._port = port

    async def close(self) -> None:
        self._log.append("policy closed")

    async def on_utterance(self, utterance) -> bool:
        await self._port.instruct_main_agent("answer")
        return False

    async def on_intervention_signal(self, agent_guid, signal) -> None:
        pass

    async def on_assistant_output(self, text, *, interrupted) -> None:
        pass

    async def wait_for_turn_to_finish(self) -> None:
        pass

    def observer_catchup_text(self) -> str:
        return ""


def test_a_lost_provider_connection_saves_memory_then_ends_the_session():
    log: list[str] = []
    client = TestClient(
        create_app(
            components_factory=DefaultSessionComponentsFactory(
                agent_factory=Factory(),
                policy_factory=lambda context: CountingPolicy(log),
            )
        )
    )

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect", "conversationId": "c1"})
        websocket.send_json(
            {"event": "user_text", "speaker": 1, "text": "hello"},
        )

        while True:
            message = websocket.receive()
            if message["type"] == "websocket.close":
                break

        # 1012, "service restart": the client reopens, and its new session
        # loads the memory this one already saved.
        assert message["code"] == 1012
        assert log == ["policy closed"]

    # Saved once: the session's own close() doesn't save again over it.
    assert log == ["policy closed"]
