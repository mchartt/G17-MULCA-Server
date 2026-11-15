"""Contract tests: the exact JSON of the five Group Coordinator events."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from muca_server.protocol import (
    CoordinatorCommandMessage,
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
    ProposalResponseMessage,
    UserTextMessage,
    parse_incoming_message,
    serialize_outgoing_message,
)


def test_proposal_response_parses_with_camel_case_fields():
    message = parse_incoming_message(
        json.dumps({"event": "proposal_response", "proposalId": "p1",
                    "action": "reject", "memberId": 7, "channel": "slack"})
    )
    assert isinstance(message, ProposalResponseMessage)
    assert (message.proposal_id, message.action, message.member_id, message.channel) == (
        "p1", "reject", 7, "slack")


def test_proposal_response_channel_defaults_to_discord():
    message = parse_incoming_message(
        {"event": "proposal_response", "proposalId": "p1", "action": "confirm", "memberId": 1}
    )
    assert message.channel == "discord"


def test_coordinator_command_parses_with_defaults():
    message = parse_incoming_message(
        {"event": "coordinator_command", "command": "state", "memberId": 3}
    )
    assert isinstance(message, CoordinatorCommandMessage)
    assert (message.args, message.request_id, message.channel) == ({}, "", "discord")


@pytest.mark.parametrize(
    "bad",
    [
        {"event": "proposal_response", "proposalId": "p1", "action": "vote", "memberId": 1},
        {"event": "proposal_response", "action": "confirm", "memberId": 1},
        {"event": "coordinator_command", "command": "delete_everything", "memberId": 1},
        {"event": "coordinator_command", "command": "state", "memberId": 1, "channel": "email"},
    ],
)
def test_invalid_coordinator_messages_are_rejected(bad):
    with pytest.raises(ValidationError):
        parse_incoming_message(bad)


def test_existing_messages_still_parse_as_before():
    message = parse_incoming_message(
        {"event": "user_text", "speaker": 1, "speakerName": "Ada", "text": "hi"}
    )
    assert isinstance(message, UserTextMessage)


def test_outgoing_events_serialize_with_their_event_names():
    assert json.loads(serialize_outgoing_message(
        CoordinatorProposalMessage(proposal={"proposal_id": "p1"})
    )) == {"event": "coordinator_proposal", "proposal": {"proposal_id": "p1"}}
    assert json.loads(serialize_outgoing_message(
        CoordinatorStateMessage(state={"charter": None}, request_id="r9")
    )) == {"event": "coordinator_state", "state": {"charter": None}, "requestId": "r9"}
    assert json.loads(serialize_outgoing_message(
        CoordinatorNoticeMessage(ok=False, text="no")
    )) == {"event": "coordinator_notice", "ok": False, "text": "no", "requestId": ""}


@pytest.mark.parametrize("action", ["take", "choose", "not_now"])
def test_m3_buttons_parse_and_choose_carries_its_option(action):
    message = parse_incoming_message(
        {"event": "proposal_response", "proposalId": "p1", "action": action,
         "memberId": 1, "option": 1, "channel": "web"}
    )
    assert (message.action, message.option) == (action, 1)


def test_m1_clicks_still_parse_without_an_option():
    message = parse_incoming_message(
        {"event": "proposal_response", "proposalId": "p1", "action": "confirm", "memberId": 1}
    )
    assert message.option is None


@pytest.mark.parametrize("command", ["planning", "checkin", "task", "why"])
def test_m4_commands_parse(command):
    message = parse_incoming_message({"event": "coordinator_command", "command": command, "memberId": 1})
    assert message.command == command
