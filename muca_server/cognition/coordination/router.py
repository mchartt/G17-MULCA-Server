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
  coordinator_command  profile | kickoff | state | planning | checkin | task | why | card
                       | language (the language of the coordinator's texts)
                       | charter  (a member edits the charter: a card to confirm)
                       | link | unlink | catchup   (linked groups, links.py and catchup.py)
  proposal_response    confirm | reject | take | choose | not_now | helpful
and it answers with coordinator_proposal / coordinator_state / coordinator_notice.

Linked groups (links.py): when a Discord channel is linked to a web space,
the store is the web space's (the group's "home") and every card and state
goes to all the group's open sessions, not only this one. Without `links`
(tests, or the feature not wired) everything is exactly as before.

It is also the session's worker in place of the Conversation (start/close
below, plugged in by session_components_factory.py), so the timed check-in
(checkin_timer.py) starts and stops with the session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import ValidationError

from ...protocol.coordinator import (
    CoordinatorCommandMessage,
    CoordinatorMirrorMessage,
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
    ProposalResponseMessage,
)
from .card_requests import CardRequests
from .catchup import CatchUp, channel_of, summary_line
from .checkin_timer import CheckinTimer
from .links import GroupLinks, LinkError, LiveGroups
from .models import CharterPayload, MemberProfile, MilestoneDraft, Proposal, ProposalKind
from .plan_commands import PlanCommands
from .plan_writer import PlanWriter
from .proactive import ProactiveBridge
from .proactive_writer import ProactiveWriter
from .proposals import ProposalService
from .store import CoordinationStore
from .writer import LANGUAGES, MAX_RATIONALE_CHARS, ProposalWriter

logger = logging.getLogger(__name__)

UNAVAILABLE = "The coordinator is not available right now. Try again in a few minutes."
GROUP_ONLY = "The coordinator works only in a group channel, not in a direct message."
MEMBERS_ONLY = "Only members of this group can use the coordinator here."
KICKOFF_RUNNING = "A kickoff is already running for this group: its card will appear here."


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
        proactive_writer: ProactiveWriter | None = None,
        checkin_interval_seconds: float = 0.0,
        links: GroupLinks | None = None,
        live: LiveGroups | None = None,
        catchup: CatchUp | None = None,
    ) -> None:
        # Set first: __getattr__ below reads it.
        self._conversation = conversation
        self._outbound = outbound_queue
        self._store_for = store_for
        # This group's writers: every model call follows the language the
        # group chose (command "language"), read from its store at each call.
        writer = writer.for_group(self._language)
        if proactive_writer is not None:
            proactive_writer = proactive_writer.for_group(self._language)
        self._writer = writer
        self._ttl = proposal_ttl_seconds
        self._today = today
        self._store: CoordinationStore | None = None
        # Linked groups (links.py). None: this session is its own group.
        self._links = links
        self._live = live
        self._catchup = catchup
        self._home_id: str | None = None
        # The kickoff running in the background, if any (one per group).
        self._kickoff: asyncio.Task | None = None
        # M3: proactive cards (CS5, CS2, CS3), plugged into the policy by wiring.py.
        self.proactive = (
            ProactiveBridge(
                conversation,
                writer=proactive_writer,
                store=self._store_now,
                service=self._service,
                send_proposal=self._send_proposal,
                members=self._roster,
                today=today,
            )
            if proactive_writer is not None
            else None
        )
        # M4: planning, checkin, task, why (plan_commands.py).
        self._plan = PlanCommands(
            participants=self._roster,
            store=self._store_now,
            service=self._service,
            writer=PlanWriter(writer),
            send_proposal=self._send_proposal,
            notice=self._notice,
            send_state=self._send_state,
            unavailable=self._fail,
            today=today,
        )
        # Helpful and "send me this card again": no model, no state change.
        self._cards = CardRequests(store=self._store_now, notice=self._notice)
        # FR15: the check-in on time, while the session is open (off when 0).
        self.checkin_timer = CheckinTimer(
            conversation,
            interval_seconds=checkin_interval_seconds,
            store=self._store_now,
            run_checkin=self._plan.timed_checkin,
        )

    def __getattr__(self, name: str) -> Any:
        """Everything not defined here is the Conversation's (see module docstring)."""
        return getattr(self._conversation, name)

    async def start(self) -> None:
        """Session worker: the Conversation first, then the check-in timer."""
        await self._conversation.start()
        self._join_live()
        self.checkin_timer.start()

    async def close(self) -> None:
        """Session worker: stop the timer, then close the Conversation as before."""
        await self.checkin_timer.close()
        self._leave_live()
        await self._conversation.close()

    async def route_user_text(self, message: Any) -> None:
        """
        A chat message: the Conversation handles it as always, then the
        group's other channels get a copy to show (linked groups only).
        """
        await self._conversation.route_user_text(message)
        try:
            await self._mirror(message)
        except Exception:  # showing a copy elsewhere must never break the chat
            logger.exception("%s could not mirror a message", self._conversation.conversation_id)

    async def _mirror(self, message: Any) -> None:
        if self._conversation.is_private or self._conversation.is_ephemeral:
            return  # a direct message never leaves its own channel
        peers = self._peers()
        text = (getattr(message, "text", "") or "").strip()
        if not peers or not text:
            return
        copy = CoordinatorMirrorMessage(
            channel=channel_of(self._conversation.conversation_id),
            speaker=message.speaker,
            speaker_name=message.speaker_name or self._conversation.participants().get(message.speaker, "member"),
            text=text,
            at=time.time(),
        )
        for peer in peers:
            await peer._outbound.put(copy)

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
        # Every command and click needs a member of the group (the client
        # sends the roster first). This also stops non-members from
        # starting paid model calls or reading the project state.
        if message.member_id not in self._conversation.participants():
            await self._notice(False, MEMBERS_ONLY, request_id)
            return
        try:
            if isinstance(message, ProposalResponseMessage) and message.action == "helpful":
                await self._cards.helpful(message)
            elif isinstance(message, ProposalResponseMessage):
                await self._on_response(message)
            elif message.command == "card":
                await self._cards.card(message)
            elif message.command == "profile":
                await self._on_profile(message)
            elif message.command == "kickoff":
                await self._start_kickoff(message)
            elif message.command in ("planning", "checkin", "task", "why", "ask"):
                await self._plan.handle(message)
            elif message.command == "language":
                await self._on_language(message)
            elif message.command == "charter":
                await self._on_charter(message)
            elif message.command == "link":
                await self._on_link(message)
            elif message.command == "unlink":
                await self._on_unlink(message)
            elif message.command == "catchup":
                await self._on_catchup(message)
            else:  # "state"
                await self._send_state(request_id)
        except Exception:
            await self._fail(message.event, request_id)

    async def wait_for_background(self) -> None:
        """Wait for a running kickoff or proactive draft (used by tests and at shutdown)."""
        if self._kickoff is not None:
            await asyncio.gather(self._kickoff, return_exceptions=True)
        if self.proactive is not None:
            await self.proactive.wait()
        await self._plan.wait()

    async def _fail(self, event: str, request_id: str) -> None:
        """Project rule: a coordinator error never breaks the chat."""
        logger.exception(
            "%s coordinator failed on %s",
            self._conversation.conversation_id,
            event,
        )
        await self._notice(False, UNAVAILABLE, request_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _on_profile(self, message: CoordinatorCommandMessage) -> None:
        """A member writes their own profile (their action, not the model's)."""
        participants = self._conversation.participants()
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

    async def _language(self) -> str:
        """The language this group chose ("" = none). Never stops a model call."""
        try:
            return await self._store_now().setting("language")
        except Exception:
            logger.exception("%s could not read the group's language", self._conversation.conversation_id)
            return ""

    async def _on_language(self, message: CoordinatorCommandMessage) -> None:
        """A member chooses the language of the coordinator's texts (their action)."""
        code = str(message.args.get("language", "")).strip().lower()
        if code == "auto":
            code = ""
        if code and code not in LANGUAGES:
            await self._notice(False, "Choose English (en), Italian (it) or auto.", message.request_id)
            return
        await self._store_now().save_setting("language", code)
        text = f"Cadence now writes in {LANGUAGES[code]}." if code else "Cadence now follows the language of the chat."
        await self._notice(True, text, message.request_id)
        await self._send_state("")

    async def _on_charter(self, message: CoordinatorCommandMessage) -> None:
        """
        A member edits the charter by hand: goal and deliverables as they
        wrote them, the milestones as they are. No model call. Like every
        change to the charter it becomes a card, and it is saved only when
        a member confirms it.
        """
        state = await self._store_now().load_state()
        deliverables = message.args.get("deliverables", [])
        if isinstance(deliverables, str):  # one per line, or ";"-separated, from a slash command
            deliverables = deliverables.replace(";", "\n").split("\n")
        try:
            payload = CharterPayload(
                goal=" ".join(str(message.args.get("goal", "")).split()),
                deliverables=[" ".join(str(d).split()) for d in deliverables if str(d).strip()],
                milestones=[MilestoneDraft(title=m.title, due_date=m.due_date) for m in state.milestones],
            )
        except (ValidationError, TypeError) as exc:
            reason = _first_error(exc) if isinstance(exc, ValidationError) else "write the deliverables as a list"
            await self._notice(False, f"Charter not changed: {reason}", message.request_id)
            return
        if state.charter is not None and (payload.goal, payload.deliverables) == (state.charter.goal, state.charter.deliverables):
            await self._notice(False, "Nothing changed: the goal and deliverables are the same as now.", message.request_id)
            return
        name = self._roster().get(message.member_id, "A member")
        proposal = await self._service().create(
            ProposalKind.CHARTER,
            payload,
            rationale=f"{name} edited the goal and deliverables."[:MAX_RATIONALE_CHARS],
            origin_channel=message.channel,
        )
        await self._send_proposal(proposal)
        await self._notice(True, "Charter card posted: it is saved when a member confirms it.", message.request_id)

    async def _start_kickoff(self, message: CoordinatorCommandMessage) -> None:
        """
        Start a kickoff without waiting for it. The session reads its
        messages one at a time (session_runtime.py receive_loop): waiting
        here for the model would freeze the group's chat and voice. One
        kickoff at a time per group, so a retry cannot make two cards.
        """
        text = str(message.args.get("text", "")).strip()
        if not text:
            await self._notice(False, "Paste the text of the assignment to start.", message.request_id)
            return
        if self._kickoff is not None and not self._kickoff.done():
            await self._notice(False, KICKOFF_RUNNING, message.request_id)
            return
        self._kickoff = asyncio.create_task(self._run_kickoff(message, text))

    async def _run_kickoff(self, message: CoordinatorCommandMessage, text: str) -> None:
        """Assignment text in, charter proposal card out (runs in the background)."""
        try:
            await self._on_kickoff(message, text)
        except Exception:
            await self._fail(message.event, message.request_id)

    async def _on_kickoff(self, message: CoordinatorCommandMessage, text: str) -> None:
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
        await self._notice(
            True,
            "Charter card posted: any member can confirm or reject it.",
            message.request_id,
        )

    async def _on_response(self, message: ProposalResponseMessage) -> None:
        outcome = await self._service().respond(
            message.proposal_id,
            action=message.action,
            member_id=message.member_id,
            participants=self._roster(),
            option=message.option,
        )
        if outcome.proposal is not None:
            # Every channel redraws the card (applied, rejected, expired...).
            await self._send_proposal(outcome.proposal)
        await self._notice(outcome.ok, outcome.message, message.request_id)
        if outcome.ok:
            await self._send_state("")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _store_now(self) -> CoordinationStore:
        """The store of this group (its home's file), opened on first use."""
        if self._store is None:
            self._store = self._store_for(self._home())
            self._join_live()
        return self._store

    def _home(self) -> str:
        """The conversation that holds the project state: the web space if linked."""
        if self._home_id is None:
            cid = self._conversation.conversation_id
            self._home_id = self._links.home(cid) if self._links is not None else cid
        return self._home_id

    def _join_live(self) -> None:
        if self._live is not None:
            self._live.join(self._home(), self)

    def _leave_live(self) -> None:
        if self._live is not None and self._home_id is not None:
            self._live.leave(self._home_id, self)

    def _rehome(self) -> None:
        """After link/unlink: forget the old home, pick up the new one."""
        self._leave_live()
        self._home_id = None
        self._store = None
        self._store_now()

    def _peers(self) -> list["CoordinatorRouter"]:
        """The other open sessions of this group (linked channels), not this one."""
        if self._live is None:
            return []
        return [r for r in self._live.of(self._home()) if r is not self]

    def _roster(self) -> dict[int, str]:
        """Everyone in the group's open sessions: this channel's members first."""
        roster: dict[int, str] = {}
        for peer in self._peers():
            roster.update(peer._conversation.participants())
        roster.update(self._conversation.participants())
        return roster

    def _names(self) -> dict[str, str]:
        """
        Member id -> name for the whole group, sent with every card and state
        (additive). A channel cannot mention a member of another channel, so
        it writes this name instead.
        """
        return {str(member_id): name for member_id, name in self._roster().items()}

    # ------------------------------------------------------------------
    # Linked groups
    # ------------------------------------------------------------------

    async def _on_link(self, message: CoordinatorCommandMessage) -> None:
        if self._links is None:
            await self._notice(False, "Linking channels is not available on this server.", message.request_id)
            return
        try:
            home = self._links.link(
                self._conversation.conversation_id,
                str(message.args.get("code", "")),
                member_id=message.member_id,
            )
        except LinkError as exc:
            await self._notice(False, str(exc), message.request_id)
            return
        self._rehome()
        logger.info("%s linked to %s", self._conversation.conversation_id, home)
        await self._notice(
            True,
            "Linked to the web space: plan, tasks, decisions and cards are now shared. "
            "Each channel keeps its own chat.",
            message.request_id,
        )
        await self._send_state(message.request_id)

    async def _on_unlink(self, message: CoordinatorCommandMessage) -> None:
        if self._links is None or self._links.unlink(self._conversation.conversation_id) is None:
            await self._notice(False, "This channel is not linked to a web space.", message.request_id)
            return
        self._rehome()
        await self._notice(
            True,
            "Unlinked: this channel has its own plan again. The web space keeps the shared one.",
            message.request_id,
        )
        await self._send_state(message.request_id)

    async def _on_catchup(self, message: CoordinatorCommandMessage) -> None:
        if self._catchup is None:
            await self._notice(False, "Catch up is not available on this server.", message.request_id)
            return
        since = message.args.get("since")
        group = self._links.group(self._home()) if self._links is not None else [self._home()]
        result = await self._catchup.build(
            store=self._store_now(),
            conversation_id=self._conversation.conversation_id,
            group=group,
            member_id=message.member_id,
            since=float(since) if isinstance(since, (int, float)) else None,
        )
        await self._outbound.put(
            CoordinatorNoticeMessage(
                ok=True, text=summary_line(result), request_id=message.request_id, catchup=result
            )
        )

    def _service(self) -> ProposalService:
        return ProposalService(self._store_now(), ttl_seconds=self._ttl)

    async def _send_state(self, request_id: str) -> None:
        state = (await self._store_now().load_state()).to_wire()
        state["names"] = self._names()
        await self._outbound.put(CoordinatorStateMessage(state=state, request_id=request_id))
        # Linked channels follow the same state (they did not ask: no request id).
        for peer in self._peers():
            await peer._outbound.put(CoordinatorStateMessage(state=state))

    async def _send_proposal(self, proposal: Proposal) -> None:
        wire = proposal.to_wire()
        wire["names"] = self._names()
        await self._outbound.put(CoordinatorProposalMessage(proposal=wire))
        for peer in self._peers():
            await peer._outbound.put(CoordinatorProposalMessage(proposal=wire))

    async def _notice(self, ok: bool, text: str, request_id: str, proposal: dict | None = None) -> None:
        await self._outbound.put(
            CoordinatorNoticeMessage(ok=ok, text=text, request_id=request_id, proposal=proposal)
        )


def _first_error(exc: ValidationError) -> str:
    """The first validation problem, in a form a student can read."""
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error.get("loc", ()))
    return f"{field}: {error.get('msg', 'invalid')}"
