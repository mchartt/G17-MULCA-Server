"""
The on/off switch. OFF (the default) must mean: MUCA-Server exactly as before.
ON: the whole path works over a real WebSocket, with a fake model.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from muca_server import create_app
from muca_server.agents import Conversation
from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.settings import CoordinatorSettings
from muca_server.cognition.coordination.wiring import build_coordinator_factory
from muca_server.events.dispatcher import DispatchResult, dispatch
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import DefaultSessionComponentsFactory

from .conftest import GOOD_CHARTER, FakeChatClient

# The existing test helpers for a session with no provider.
from test_text_conversation import AlwaysAnswer, ScriptedAgentFactory


def test_the_switch_is_off_by_default():
    assert CoordinatorSettings.from_env({}) == CoordinatorSettings()
    assert not CoordinatorSettings.from_env({"MUCA_COORDINATOR_ENABLED": "no"}).enabled
    on = CoordinatorSettings.from_env(
        {"MUCA_COORDINATOR_ENABLED": "True", "MUCA_COORDINATOR_PROPOSAL_TTL_HOURS": "2"}
    )
    assert on.enabled and on.proposal_ttl_seconds == 7200


def test_off_builds_no_coordinator(tmp_path):
    assert build_coordinator_factory(
        CoordinatorSettings(enabled=False), client=None, db_dir=str(tmp_path)
    ) is None


async def test_off_the_session_router_is_the_plain_conversation():
    from muca_server.sessions.session_components import SessionContext
    from muca_server.sessions.session_state import SessionState
    import asyncio
    from uuid import uuid4

    session_id = uuid4()
    factory = DefaultSessionComponentsFactory(agent_factory=ScriptedAgentFactory())
    components = await factory.create(
        SessionContext(
            session_id=session_id,
            websocket=None,  # not used to build the components
            state=SessionState(session_id=session_id),
            outbound_queue=asyncio.Queue(),
        )
    )
    assert type(components.agent_router) is Conversation
    assert components.workers == [components.agent_router]  # off: workers as before


async def test_on_the_router_takes_the_conversations_worker_slot(tmp_path):
    from muca_server.sessions.session_components import SessionContext
    from muca_server.sessions.session_state import SessionState
    import asyncio
    from uuid import uuid4

    session_id = uuid4()
    factory = DefaultSessionComponentsFactory(
        agent_factory=ScriptedAgentFactory(),
        coordinator_factory=build_coordinator_factory(
            CoordinatorSettings(enabled=True), client=FakeChatClient(), db_dir=str(tmp_path)
        ),
    )
    components = await factory.create(
        SessionContext(
            session_id=session_id,
            websocket=None,
            state=SessionState(session_id=session_id),
            outbound_queue=asyncio.Queue(),
        )
    )
    assert isinstance(components.agent_router, CoordinatorRouter)
    assert components.workers == [components.agent_router]


async def test_off_coordinator_messages_are_ignored_without_error():
    class PlainRouter:  # has no route_coordinator, like a Conversation
        pass

    message = parse_incoming_message(
        {"event": "coordinator_command", "command": "state", "memberId": 1}
    )
    assert await dispatch(message, PlainRouter()) is DispatchResult.CONTINUE


def test_on_kickoff_and_confirm_work_over_the_websocket(tmp_path):
    fake_client = FakeChatClient(GOOD_CHARTER)
    app = create_app(
        components_factory=DefaultSessionComponentsFactory(
            agent_factory=ScriptedAgentFactory(),
            policy_factory=lambda context: AlwaysAnswer(),
            coordinator_factory=build_coordinator_factory(
                CoordinatorSettings(enabled=True), client=fake_client, db_dir=str(tmp_path)
            ),
        )
    )
    with TestClient(app).websocket_connect("/") as ws:
        ws.send_json({"event": "connect", "conversationId": "channel:42", "isDm": False})
        ws.send_json({"event": "participants", "participants": [{"id": 1, "name": "Ada"}]})
        ws.send_json({"event": "coordinator_command", "command": "kickoff",
                      "memberId": 1, "args": {"text": "Assignment"}})
        card = next_event(ws, "coordinator_proposal")["proposal"]
        assert card["status"] == "pending"

        ws.send_json({"event": "proposal_response", "proposalId": card["proposal_id"],
                      "action": "confirm", "memberId": 1})
        assert next_event(ws, "coordinator_proposal")["proposal"]["status"] == "applied"
        state = next_event(ws, "coordinator_state")["state"]
        assert state["charter"]["goal"] == GOOD_CHARTER["goal"]

        ws.send_json({"event": "disconnect"})

    # Saved in the conversation's own SQLite file.
    assert list(tmp_path.glob("*.db"))


def test_on_the_router_wraps_the_conversation(tmp_path):
    factory = build_coordinator_factory(
        CoordinatorSettings(enabled=True), client=FakeChatClient(), db_dir=str(tmp_path)
    )
    router = factory(object(), None)
    assert isinstance(router, CoordinatorRouter)


def next_event(ws, name: str) -> dict:
    """Read frames until one with event == name (others, like usage, are skipped)."""
    for _ in range(50):
        frame = ws.receive_json()
        if frame.get("event") == name:
            return frame
    raise AssertionError(f"no {name} event")
