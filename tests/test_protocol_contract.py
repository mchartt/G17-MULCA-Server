"""
Checks the wire contract against the client's own reference, which lives in
dea-multiuser at Orchestration/Remote/OrchestrationServiceRemoteMessageReference.cs.
"""

import json

import pytest

from muca_server.protocol import (
    AgentGuidMessage,
    AgentMessage,
    StateUpdateMessage,
    TextMessage,
    TriggerResponseMessage,
    parse_incoming_message,
    serialize_outgoing_message,
)


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        (
            {
                "event": "audio_stream",
                "speaker": 1,
                "audio": "",
                "isNotifying": True,
                "playbackTimeMs": 14250,
            },
            "AudioStreamMessage",
        ),
        ({"event": "playback_frame", "audio": ""}, "PlaybackFrameMessage"),
        ({"event": "connect", "targetAgentGuid": ""}, "ConnectAgentMessage"),
        ({"event": "connect_main_assistant"}, "ConnectAgentMessage"),
        ({"event": "interrupt", "targetAgentGuid": ""}, "InterruptMessage"),
        ({"event": "interrupt_response"}, "InterruptMessage"),
        ({"event": "disconnect"}, "DisconnectMessage"),
        (
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            },
            "CreateAuxiliaryAgentMessage",
        ),
        (
            {
                "event": "remove_auxiliary_agent",
                "targetAgentGuid": "123e4567-e89b-12d3-a456-426614174000",
            },
            "RemoveAuxiliaryAgentMessage",
        ),
    ],
)
def test_every_client_event_parses(raw, expected_type):
    message = parse_incoming_message(raw)

    assert type(message).__name__ == expected_type


def test_text_message_carries_the_role():
    message = parse_incoming_message(
        {
            "event": "text_message",
            "text": "User 1 has not talked for 5 minutes",
            "role": "system",
            "targetAgentGuid": "",
        }
    )

    assert isinstance(message, TextMessage)
    assert message.role == "system"
    assert message.target_agent_guid == ""


def test_trigger_response_carries_generation_control():
    message = parse_incoming_message(
        {
            "event": "trigger_response",
            "maxTokens": 256,
            "interrupt": False,
            "instructions": "Respond with audio only",
        }
    )

    assert isinstance(message, TriggerResponseMessage)
    assert message.max_tokens == 256
    assert message.interrupt is False
    assert message.instructions == "Respond with audio only"


def test_agent_guid_message_is_named_guid_for_the_client():
    payload = json.loads(
        serialize_outgoing_message(
            AgentGuidMessage(agent_guid="987f6543-e21b-32d3-b654-826614174111"),
        )
    )

    assert payload == {
        "event": "agent_guid",
        "guid": "987f6543-e21b-32d3-b654-826614174111",
    }


def test_agent_message_omits_the_guid_for_the_main_agent():
    payload = json.loads(
        serialize_outgoing_message(AgentMessage(text="raw agent event")),
    )

    assert payload == {
        "event": "agent_message",
        "text": "raw agent event",
    }


def test_state_update_uses_the_client_field_names():
    payload = json.loads(
        serialize_outgoing_message(
            StateUpdateMessage.interrupted(
                current_speaker=1,
                last_speaker=2,
            ),
        )
    )

    assert set(payload) == {
        "event",
        "isConnected",
        "currentSpeaker",
        "lastSpeaker",
        "isAnswering",
        "assistantSpokeLast",
        "clearAudioQueue",
        "audio",
    }
    assert payload["clearAudioQueue"] is True
