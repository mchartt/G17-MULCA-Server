"""
CoordinatorRouter: plugs the Group Coordinator into one session.

Design (Decorator pattern, from the GoF catalog): the session's normal router
is the `Conversation` (agents/conversation.py). When the coordinator is ON,
session_components_factory.py wraps that Conversation in a CoordinatorRouter:

    dispatcher --> CoordinatorRouter --route_coordinator()--> coordinator code
                         |
                         +--every other method, unchanged--> Conversation

`__getattr__` forwards every attribute the wrapper does not define to the
Conversation, so all existing messages behave exactly as before. When the
coordinator is OFF there is no wrapper at all.

What it handles:
  coordinator_command  profile | kickoff | state
  proposal_response    confirm | reject
and it answers with coordinator_proposal / coordinator_state / coordinator_notice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import ValidationError

from ...protocol.coordinator import (
    CoordinatorCommandMessage,
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
    ProposalResponseMessage,
)
from .models import MemberProfile, Proposal, ProposalKind
from .proposals import ProposalService
from .store import CoordinationStore
from .writer import ProposalWriter

logger = logging.getLogger(__name__)

UNAVAILABLE = "The coordinator is not available right now. Try again in a few minutes."
GROUP_ONLY = "The coordinator works only in a group channel, not in a direct message."


class CoordinatorRouter:
    def __init__(
        self,
        conversation: Any,
        outbound_queue: asyncio.Queue,
        *,
        store_for: Callable[[str], CoordinationStore],
        writer: ProposalWriter,
        proposal_ttl_seconds: float,
        today: Callable[[], date] = date.today,
    ) -> None:
        # Set first: __getattr__ below reads it.
        self._conversation = conversation
        self._outbound = outbound_queue
        self._store_for = store_for
        self._writer = writer
        self._ttl = proposal_ttl_seconds
        self._today = today
        self._store: CoordinationStore | None = None

    def __getattr__(self, name: str) -> Any:
        """Everything not defined here is the Conversation's (see module docstring)."""
        return getattr(self._conversation, name)

    # ------------------------------------------------------------------
    # Entry point, called by events/dispatcher.py
    # ------------------------------------------------------------------

    async def route_coordinator(
        self,
        message: CoordinatorCommandMessage | ProposalResponseMessage,
    ) -> None:
        request_id = getattr(message, "request_id", "")
        if self._conversation.is_private or self._conversation.is_ephemeral:
            await self._notice(False, GROUP_ONLY, request_id)
            return
        try:
            if isinstance(message, ProposalResponseMessage):
                await self._on_response(message)
            elif message.command == "profile":
                await self._on_profile(message)
            elif message.command == "kickoff":
                await self._on_kickoff(message)
            else:  # "state"
                await self._send_state(request_id)
        except Exception:
            # Project rule: a coordinator error never breaks the chat.
            logger.exception(
                "%s coordinator failed on %s",
                self._conversation.conversation_id,
                message.event,
            )
            await self._notice(False, UNAVAILABLE, request_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _on_profile(self, message: CoordinatorCommandMessage) -> None:
        """A member writes their own profile (their action, not the model's)."""
        participants = self._conversation.participants()
        if message.member_id not in participants:
            await self._notice(False, "Only members of this group can have a profile.", message.request_id)
            return
        args = message.args
        skills = args.get("skills", [])
        if isinstance(skills, str):  # "python, react" from a slash command
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        try:
            profile = MemberProfile(
                member_id=message.member_id,
                display_name=args.get("display_name") or participants[message.member_id],
                discipline=args.get("discipline", ""),
                skills=skills,
                weekly_hours=args.get("weekly_hours"),
            )
        except ValidationError as exc:
            await self._notice(False, f"Profile not saved: {_first_error(exc)}", message.request_id)
            return
        await self._store_now().upsert_member(profile)
        await self._notice(True, "Profile saved.", message.request_id)
        await self._send_state(message.request_id)

    async def _on_kickoff(self, message: CoordinatorCommandMessage) -> None:
        """Assignment text in, charter proposal card out."""
        text = str(message.args.get("text", "")).strip()
        if not text:
            await self._notice(False, "Paste the text of the assignment to start.", message.request_id)
            return
        draft = await self._writer.draft_charter(text, today=self._today())
        if draft is None:
            await self._notice(False, UNAVAILABLE, message.request_id)
            return
        proposal = await self._service().create(
            ProposalKind.CHARTER,
            draft.payload,
            rationale=draft.rationale,
            origin_channel=message.channel,
        )
        await self._send_proposal(proposal)

    async def _on_response(self, message: ProposalResponseMessage) -> None:
        outcome = await self._service().respond(
            message.proposal_id,
            action=message.action,
            member_id=message.member_id,
            participants=self._conversation.participants(),
        )
        if outcome.proposal is not None:
            # Every channel redraws the card (applied, rejected, expired...).
            await self._send_proposal(outcome.proposal)
        await self._notice(outcome.ok, outcome.message, "")
        if outcome.ok:
            await self._send_state("")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _store_now(self) -> CoordinationStore:
        """The store for this conversation, opened on first use."""
        if self._store is None:
            self._store = self._store_for(self._conversation.conversation_id)
        return self._store

    def _service(self) -> ProposalService:
        return ProposalService(self._store_now(), ttl_seconds=self._ttl)

    async def _send_state(self, request_id: str) -> None:
        state = await self._store_now().load_state()
        await self._outbound.put(
            CoordinatorStateMessage(state=state.to_wire(), request_id=request_id)
        )

    async def _send_proposal(self, proposal: Proposal) -> None:
        await self._outbound.put(CoordinatorProposalMessage(proposal=proposal.to_wire()))

    async def _notice(self, ok: bool, text: str, request_id: str) -> None:
        await self._outbound.put(
            CoordinatorNoticeMessage(ok=ok, text=text, request_id=request_id)
        )


def _first_error(exc: ValidationError) -> str:
    """The first validation problem, in a form a student can read."""
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error.get("loc", ()))
    return f"{field}: {error.get('msg', 'invalid')}"
