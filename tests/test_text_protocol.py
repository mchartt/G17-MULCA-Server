"""The text-modality half of the wire contract, used by the Discord adapter."""

import json

from muca_server.protocol import (
    AssistantTextMessage,
    ParticipantsMessage,
    UserTextMessage,
    parse_incoming_message,
    serialize_outgoing_message,
)


def test_a_user_utterance_carries_who_said_it():
    message = parse_incoming_message(
        {
            "event": "user_text",
            "speaker": 42,
            "speakerName": "Ada",
            "text": "morning all",
        }
    )

    assert isinstance(message, UserTextMessage)
    assert message.speaker == 42
    assert message.speaker_name == "Ada"
    assert message.text == "morning all"


def test_a_user_utterance_survives_a_missing_name():
    message = parse_incoming_message(
        {"event": "user_text", "speaker": 1, "text": "hi"},
    )

    assert message.speaker_name == ""


def test_participants_carry_people_who_have_not_spoken():
    message = parse_incoming_message(
        {
            "event": "participants",
            "participants": [
                {"id": 1, "name": "Ada"},
                {"id": 2, "name": "Grace"},
            ],
        }
    )

    assert isinstance(message, ParticipantsMessage)
    assert [p.name for p in message.participants] == ["Ada", "Grace"]


def test_connect_carries_the_conversation_and_its_privacy():
    message = parse_incoming_message(
        {
            "event": "connect",
            "conversationId": "channel-7",
            "isDm": True,
        }
    )

    assert message.conversation_id == "channel-7"
    assert message.is_dm is True


def test_connect_still_works_without_the_new_fields():
    message = parse_incoming_message(
        {"event": "connect_main_assistant"},
    )

    assert message.conversation_id is None
    assert message.is_dm is False


def test_assistant_text_streams_then_closes():
    delta = json.loads(
        serialize_outgoing_message(AssistantTextMessage.delta("hel")),
    )
    final = json.loads(
        serialize_outgoing_message(AssistantTextMessage.final()),
    )

    assert delta == {
        "event": "assistant_text",
        "text": "hel",
        "isFinal": False,
        "modality": "text",
        "interrupted": False,
    }
    assert final["isFinal"] is True
