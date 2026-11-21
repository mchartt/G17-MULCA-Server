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
    # Linked groups (additive): link (args.code: the web space's invite code,
    # run in a Discord channel), unlink, catchup (args.since: optional unix
    # time; what the member missed in the group's other channels).
    # ask (additive): args.question, any question about the saved tasks,
    # milestones and decisions; answered with a notice that names its sources.
    # language (additive): args.language "en" | "it" | "" (follow the chat):
    # the language of everything the coordinator writes for this group.
    # task (additive): besides args.status, a member may set args.owner (a
    # member id, or "" for nobody), args.due (YYYY-MM-DD, or ""), args.title;
    # args.add = the title of a new task; args.remove = true drops the task.
    # milestone (additive): args.add = title (+ args.due); or args.milestone =
    # its id with args.title / args.due / args.remove. By hand: no card.
    # planning (additive): args.items = [{title, owner_id?, due_date?}] instead
    # of args.text; an owner the members chose is kept, the others are suggested.
    # charter (additive): args.goal, args.deliverables: a member edits the
    # confirmed charter; answered with a charter card to confirm, no model.
    # approach (additive, CS-OPEN-01): no args; "Plan together": Cadence asks the
    # group how it wants to plan the work, then sums the answers up in a card.
    # document (additive): args.add = {name, text} keeps a document's text as
    # project context; args.remove = its id. kickoff may also carry
    # args.documents = [{name, text}] and args.description, kept the same way.
    # reflection (additive): args.week = a YYYY-MM-DD day of the week (default
    # this week) -> a notice with `reflection` (reflection.py); group-level only.
    # progress (additive, CS6): no args; the adapter asks for one progress
    # assessment now (a group nobody has opened since a restart has no timer
    # running). Answered with an empty notice; a question, if any, is posted
    # in the chat as an assistant message.
    command: Literal[
        "profile", "kickoff", "state", "planning", "checkin", "task", "why", "card",
        "link", "unlink", "catchup", "ask", "language", "charter", "milestone",
        "approach", "document", "reflection", "progress",
        # "me": one person, one member across channels (router._on_me).
        "me",
    ]
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
    # Only for the "catchup" command (additive): decisions and messages from
    # the group's other channels since the member's last catch-up (catchup.py).
    catchup: dict[str, Any] | None = None
    # Only for the "reflection" command (additive): the week's facts and topics.
    reflection: dict[str, Any] | None = None


class CoordinatorMirrorMessage(OutgoingMessage):
    """
    Linked groups: a message a member wrote in another channel of the group
    (e.g. on Discord, shown on the web). The client shows it with the channel's
    name; it is not sent to this session's agent.
    """

    event: Literal["coordinator_mirror"] = "coordinator_mirror"
    channel: str  # "web" | "discord" | "slack"
    speaker: int
    speaker_name: str = Field(serialization_alias="speakerName")
    text: str
    at: float  # unix time

