"""
Group Coordinator messages, added to the protocol without changing any
existing message.

Client -> server:
  proposal_response    a member clicked a button on a proposal card
  coordinator_command  a member ran a coordinator command (profile, kickoff, state,
                       planning, checkin, task, why)

Server -> client:
  coordinator_proposal  a proposal card to show, or an update of one
  coordinator_state     the project state (charter, members, plan, decisions)
  coordinator_notice    a short text answer to one command (ok or not)

A client that does not know these server events ignores them
(mulca_discord/muca_client.py, the final `else`). A server with the
coordinator switched off logs the client events and drops them
(events/dispatcher.py). See docs/PROTOCOL.md, section "Group Coordinator".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .outgoing import OutgoingMessage

# Which channel a member used, so the web app can show a badge.
Channel = Literal["discord", "web", "slack", "calendar"]


# ----------------------------------------------------------------------
# Client -> server
# ----------------------------------------------------------------------


class CoordinatorIncomingMessage(BaseModel):
    """
    Same settings as incoming.IncomingMessage. Not a subclass of it, so that
    incoming.py can import this module without a circular import.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ProposalResponseMessage(CoordinatorIncomingMessage):
    """A member's click on a proposal card."""

    event: Literal["proposal_response"]
    proposal_id: str = Field(alias="proposalId")
    # M1: confirm, reject. M3 adds (additive): take ("I'll take it"),
    # choose (one option of a decision card, see `option`) and not_now.
    # Later (additive): helpful (feedback on a proactive card, changes nothing).
    action: Literal["confirm", "reject", "take", "choose", "not_now", "helpful"]
    # For "choose": the index of the chosen option on the card.
    option: int | None = None
    # The clicking member's participant id (Participant.id).
    member_id: int = Field(alias="memberId")
    channel: Channel = "discord"
    # Echoed back in the notice, so the client can answer the right click.
    request_id: str = Field(default="", alias="requestId")


class CoordinatorCommandMessage(CoordinatorIncomingMessage):
    """A coordinator command typed by a member, with its arguments."""

    event: Literal["coordinator_command"]
    # M4 adds (additive): planning, checkin, task (a member updates a task's
    # status), why (a question about a past decision). Later (additive):
    # card (args.proposal_id: send that card back to the asker, e.g. for
    # "Why?" after a client restart).
    command: Literal["profile", "kickoff", "state", "planning", "checkin", "task", "why", "card"]
    member_id: int = Field(alias="memberId")
    args: dict[str, Any] = Field(default_factory=dict)
    channel: Channel = "discord"
    # Echoed back in the answer, so the client knows which command it answers.
    request_id: str = Field(default="", alias="requestId")


# ----------------------------------------------------------------------
# Server -> client
# ----------------------------------------------------------------------


class CoordinatorProposalMessage(OutgoingMessage):
    """
    One proposal, as the clients draw it. Sent when it is created and again
    every time its status changes, so every channel updates its card.
    """

    event: Literal["coordinator_proposal"] = "coordinator_proposal"
    proposal: dict[str, Any]


class CoordinatorStateMessage(OutgoingMessage):
    """The whole project state, after any change or when asked for."""

    event: Literal["coordinator_state"] = "coordinator_state"
    state: dict[str, Any]
    request_id: str = Field(default="", serialization_alias="requestId")


class CoordinatorNoticeMessage(OutgoingMessage):
    """A short answer to one command: what happened, or why not."""

    event: Literal["coordinator_notice"] = "coordinator_notice"
    ok: bool
    text: str
    request_id: str = Field(default="", serialization_alias="requestId")
    # Only for the "card" command (additive): the card asked for.
    proposal: dict[str, Any] | None = None  # dropped when None (serialize_outgoing_message uses exclude_none)
