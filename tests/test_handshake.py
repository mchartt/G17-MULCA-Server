"""
The connect handshake must apply conversation_id/is_dm before anything else
runs, not just gate the 5-second identification timeout.

Regression test for a bug where SessionRuntime parsed the handshake message
only far enough to read its `event` field, then discarded it -- so a client
setting `conversationId`/`isDm` on that same message (as mulca_discord does,
to key scope and mark DMs private) had it silently ignored.
"""

from fastapi.testclient import TestClient

from muca_server.main import create_app
from muca_server.sessions import DefaultSessionComponentsFactory
from muca_server.sessions.session_runtime import SessionRuntime


class RecordingPolicy:
    """Cognition that does nothing but remember what the port told it."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def start(self, port) -> None:
        self._sink.append(
            {
                "conversation_id": port.conversation_id,
                "is_private": port.is_private,
            }
        )

    async def close(self) -> None:
        pass

    async def on_utterance(self, utterance) -> bool:
        return True

    async def on_intervention_signal(self, agent_guid, signal) -> None:
        pass

    async def on_assistant_output(self, text, *, interrupted) -> None:
        pass


def test_connect_message_conversation_id_and_is_dm_reach_the_policy():
    recorded: list = []
    components_factory = DefaultSessionComponentsFactory(
        policy_factory=lambda context: RecordingPolicy(recorded),
    )
    client = TestClient(create_app(components_factory=components_factory))

    with client.websocket_connect("/") as websocket:
        websocket.send_json(
            {
                "event": "connect",
                "conversationId": "dm:42",
                "isDm": True,
            }
        )
        websocket.send_json({"event": "disconnect"})
        assert websocket.receive()["type"] == "websocket.close"

    assert recorded == [{"conversation_id": "dm:42", "is_private": True}]


def test_connect_message_without_identity_still_works():
    recorded: list = []
    components_factory = DefaultSessionComponentsFactory(
        policy_factory=lambda context: RecordingPolicy(recorded),
    )
    client = TestClient(create_app(components_factory=components_factory))

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect_main_assistant"})
        websocket.send_json({"event": "disconnect"})
        assert websocket.receive()["type"] == "websocket.close"

    assert len(recorded) == 1
    assert recorded[0]["is_private"] is False


def test_a_session_over_the_limit_is_refused_before_any_agent_is_built(
    monkeypatch,
):
    recorded: list = []
    components_factory = DefaultSessionComponentsFactory(
        policy_factory=lambda context: RecordingPolicy(recorded),
    )
    client = TestClient(create_app(components_factory=components_factory))
    monkeypatch.setattr(SessionRuntime, "max_sessions", 0)

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect", "conversationId": "a"})
        closed = websocket.receive()

    assert closed["type"] == "websocket.close"
    assert closed["code"] == 1013
    assert recorded == []
    assert SessionRuntime.active_sessions == 0


def test_a_closed_session_no_longer_counts_as_active():
    components_factory = DefaultSessionComponentsFactory(
        policy_factory=lambda context: RecordingPolicy([]),
    )
    client = TestClient(create_app(components_factory=components_factory))

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect", "conversationId": "a"})
        websocket.send_json({"event": "disconnect"})
        assert websocket.receive()["type"] == "websocket.close"

    assert SessionRuntime.active_sessions == 0
