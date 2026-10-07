"""
A session survives what goes wrong inside it: one failed request is
reported to the client, not fatal, and the session only shuts down once.
"""

from fastapi.testclient import TestClient

from muca_server import create_app
from muca_server.agents import AuxiliaryAgent, MainAgent
from muca_server.sessions import DefaultSessionComponentsFactory


class ReplyingMainAgent(MainAgent):
    session_updates: list[dict] = []

    async def _trigger_response(self, **kwargs) -> None:
        await self._stream_text("ok", is_final=True)

    async def _update_session(self, session) -> None:
        ReplyingMainAgent.session_updates.append(session)


class BrokenAuxiliaryAgent(AuxiliaryAgent):
    async def _connect(self) -> None:
        raise ConnectionError("provider refused")


class Factory:
    def build_main(self, options):
        return ReplyingMainAgent(options=options)

    def build_auxiliary(self, options):
        return BrokenAuxiliaryAgent(options=options)


class AnswerEverything:
    closes = 0

    async def start(self, port) -> None:
        self._port = port

    async def close(self) -> None:
        AnswerEverything.closes += 1

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


def make_client() -> TestClient:
    return TestClient(
        create_app(
            components_factory=DefaultSessionComponentsFactory(
                agent_factory=Factory(),
                policy_factory=lambda context: AnswerEverything(),
            )
        )
    )


def receive_until(websocket, event: str) -> dict:
    while True:
        message = websocket.receive_json()
        if message["event"] == event:
            return message


def test_a_failed_request_is_reported_and_the_session_carries_on():
    with make_client().websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect"})
        websocket.send_json(
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            }
        )

        error = receive_until(websocket, "error")
        assert "create_auxiliary_agent" in error["message"]

        websocket.send_json(
            {"event": "user_text", "speaker": 1, "text": "still there?"},
        )
        reply = receive_until(websocket, "assistant_text")
        assert reply["text"] == "ok"

        websocket.send_json({"event": "disconnect"})


def test_an_unknown_auxiliary_agent_type_is_an_error_not_a_crash():
    with make_client().websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect"})
        websocket.send_json(
            {"event": "create_auxiliary_agent", "auxiliaryAgentType": "nope"},
        )

        assert "nope" in receive_until(websocket, "error")["message"]
        websocket.send_json({"event": "disconnect"})


def test_a_unity_provider_passthrough_frame_is_tolerated():
    with make_client().websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect_main_assistant"})
        websocket.send_json(
            {"type": "session.update", "session": {"modalities": ["audio"]}},
        )
        websocket.send_json(
            {"event": "user_text", "speaker": 1, "text": "hello"},
        )

        assert receive_until(websocket, "assistant_text")["text"] == "ok"
        websocket.send_json({"event": "disconnect"})


def test_a_clients_session_update_reaches_the_main_agent():
    """dea-multiuser's way: connect, then its own session.update frames."""

    ReplyingMainAgent.session_updates = []

    with make_client().websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect_main_assistant"})
        websocket.send_json(
            {"type": "session.update", "session": {"instructions": "You are Foxy."}},
        )
        # Only the main agent takes one.
        websocket.send_json(
            {
                "type": "session.update",
                "targetAgentGuid": "00000000-0000-0000-0000-000000000001",
                "session": {"instructions": "You are an observer."},
            },
        )
        websocket.send_json(
            {"event": "user_text", "speaker": 1, "text": "hello"},
        )

        assert receive_until(websocket, "assistant_text")["text"] == "ok"
        websocket.send_json({"event": "disconnect"})

    assert ReplyingMainAgent.session_updates == [{"instructions": "You are Foxy."}]


def test_a_disconnect_closes_the_conversation_once():
    AnswerEverything.closes = 0

    with make_client().websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect"})
        websocket.send_json({"event": "disconnect"})
        assert websocket.receive()["type"] == "websocket.close"

    assert AnswerEverything.closes == 1
