from uuid import UUID

from fastapi.testclient import TestClient

from muca_server import app


def test_a_session_rejects_a_client_that_does_not_identify():
    client = TestClient(app)

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "text_message", "text": "hello"})

        assert websocket.receive()["type"] == "websocket.close"


def test_creating_an_auxiliary_agent_answers_with_its_guid():
    client = TestClient(app)

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect_main_assistant"})
        websocket.send_json(
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            }
        )

        message = websocket.receive_json()

        websocket.send_json({"event": "disconnect"})
        assert websocket.receive()["type"] == "websocket.close"

    assert message["event"] == "agent_guid"
    UUID(message["guid"])


def test_audio_and_text_travel_without_a_provider():
    client = TestClient(app)

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect_main_assistant"})
        websocket.send_json(
            {
                "event": "audio_stream",
                "speaker": 1,
                "audio": "AAAAAA==",
                "isNotifying": False,
                "playbackTimeMs": 120,
            }
        )
        websocket.send_json(
            {
                "event": "text_message",
                "text": "User 1 has not talked for 5 minutes",
                "role": "system",
            }
        )
        # The server reports the speaker change it just worked out.
        update = websocket.receive_json()

        websocket.send_json({"event": "disconnect"})

        assert websocket.receive()["type"] == "websocket.close"

    assert update["event"] == "state_update"
    assert update["currentSpeaker"] == 1
