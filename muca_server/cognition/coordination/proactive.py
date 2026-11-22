"""
ProactiveBridge: how a coordinator strategy (CS5, CS2, CS3) becomes a card.

Where it sits (M3):

    intervention agent picks a strategy
        -> CoalaPolicy.on_intervention_signal (cognition/policy.py)
             existing vetoes (cooldown, modality)
             -> bridge.offer(...)            "is this one of yours?"
                  NOT_MINE -> the lab's normal path (main agent replies)
                  VETOED   -> silence (one of the rules below said no)
                  TAKEN    -> silence in the chat; the card is drafted in the
                              background and posted when ready

Deterministic rules, checked in code and never left to the model (project
rules 4.3 and 11, spec NFR3, GL4, GL10):

    R1  only in a group conversation, never in a DM
    R2  one draft at a time per group; a more urgent strategy (PRIORITY) that
        fires while a less urgent one is still being drafted replaces it,
        only before that draft is saved (in the 20/11 live run a CS5 card on
        "I'll look at it tonight" made the CS7 on "Mathias must first send me
        the data for the RASD" be dropped 7 s later)
    R3  at least MIN_MESSAGES_BETWEEN messages since the last proactive card
    R4  at least MIN_SECONDS_BETWEEN since the last proactive card
    R5  never right after the agent's own message (no two in a row)
    R6  "Not now": a silenced (strategy, topic) never gets a card (checked
        after the draft, when the topic is known)
    R7  CS5: no card for a task the plan already shows with an owner;
        CS7: no card for a task already marked blocked, or for a dependency
        the plan already records; K3: no card on a decision already reopened
    R8  no card while the plan opener listens for the group's answers to
        "How do you want to plan the work?" (opener.py): their replies
        ("I take the API") are answers, not gaps to fill
    R9  no second card while one on the same thing (kind and topic, or task
        title) is still waiting for a click
    R10 one responder per message: a message the client already sent to
        Cadence's ask (a web follow-up offered as "maybe") is not answered a
        second time by the lab's solicited strategies (respond_when_addressed,
        remember, provide_summary). Before this, such a follow-up could get
        two replies, the second in English from an agent that cannot see the
        plan ("I have no access beyond the chat", 20/11 screenshots). The same
        holds for a card: a message Cadence's ask already has gets no
        proactive card as well (21/11 live test: one line, "qualcuno dovrebbe
        preparare le domande entro venerdì", got two cards and a chat reply)
    R11 one voice: in a group text chat the lab's solicited strategies are
        answered by Cadence's ask (plan_commands.answer_late, a sure call),
        which reads the project, never by the lab's main agent. The lab's
        intervention agent still decides WHEN; only who writes changes. Not
        in a DM, not for a spoken turn, and the lab's path stays the fallback
        when Cadence cannot take the message
    R12 the lab's unsolicited strategies (pleasant_conversation,
        maintain_focus, resolve_conflict, participation_equality) never speak
        in a group text chat while the coordinator is on. They are left out
        of the "ambient" catalog (run_server.py), but the web runs in "wake"
        mode, whose catalog still offers them: on 21/11 pleasant_conversation
        answered "blu o verde?" in English, from the agent that cannot see
        the plan

R3 and R4 count only cards actually posted: a draft that fails or is
dropped does not silence the group. Their marker is saved in the group's
SQLite file (store.save_mark("proactive", ...)) and read back on the first offer of
a new session, so a server restart does not reset them. The policy's own cooldown
(`cooldown_messages` in the strategy YAML) still applies on top.

Linked groups (links.py): several channels share one project, and each has
its own bridge. The group must still get one unsolicited card at a time, so:
    - R4 (time) is for the whole group: the time of the last card is read
      from the shared file at every offer, whichever channel posted it;
    - R3 (messages) stays per channel, because each channel counts its own
      messages: its count is saved under "proactive:<conversation id>";
    - the members a card may name (CS5 owner) are the group's, on every
      channel (`members`, the router's roster), not only this channel's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from enum import StrEnum
from typing import Any

from .models import BlockerCertainty, BlockerPayload, Proposal, ProposalKind, TaskAssignmentPayload, TaskStatus
from .plan_writes import known_prerequisite, topic_key
from .project_memory import RESEND_EVERY, project_memory
from .proactive_writer import STRATEGY_KINDS, ChatLine, ProactiveDraft, ProactiveWriter
from .proposals import ProposalService
from .store import CoordinationStore

logger = logging.getLogger(__name__)

MIN_MESSAGES_BETWEEN = 10  # NFR3: one unsolicited intervention every 10 messages at most
MIN_SECONDS_BETWEEN = 5 * 60  # NFR3: ... and every 5 minutes at most
# What the group itself just said it needs is not held back that long (22/11
# demo run): "I'll write the guide by Tuesday", an agreed change, "app or
# kiosk, we keep going back and forth" and "I'm stuck until…" got no card
# because another card had been posted a minute before, in another channel
# too. These three record the group's own words; the long gap stays for the
# cards Cadence thinks of by itself (delegation, a decision reopened).
STATED_NEEDS = frozenset({"cs5_owner_and_date", "cs3_close_decision", "cs7_blockers_dependencies"})
STATED_MESSAGES_BETWEEN = 2
STATED_SECONDS_BETWEEN = 60
CHAT_WINDOW = 20  # recent messages the writer sees
# R2: which draft wins when two strategies fire close together. A stated
# blocker with a date at risk matters more than an open decision, which
# matters more than a missing owner or a simple task to hand out.
PRIORITY = {
    "cs7_blockers_dependencies": 3,
    "cs3_close_decision": 2,
    "cs3_reopened_decision": 2,
    "cs5_owner_and_date": 1,
    "cs2_delegate_simple_task": 0,
}


# The lab's strategies that answer a member with free text (R10).
SOLICITED = frozenset({"respond_when_addressed", "remember", "provide_summary", "always_reply"})
CLAIM_SECONDS = 120


def claim_key(text: str) -> str:
    return " ".join((text or "").lower().split())[:300]


class Verdict(StrEnum):
    NOT_MINE = "not_mine"
    VETOED = "vetoed"
    TAKEN = "taken"


class ProactiveBridge:
    def __init__(
        self,
        conversation: Any,
        *,
        writer: ProactiveWriter,
        store: Callable[[], CoordinationStore],
        service: Callable[[], ProposalService],
        send_proposal: Callable[[Proposal], Awaitable[None]],
        members: Callable[[], dict[int, str]] | None = None,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.time,
        listening: Callable[[], bool] | None = None,
        answer: Callable[[int, str], bool] | None = None,
    ) -> None:
        self._conversation = conversation
        # R11: hands a message to Cadence's ask (router); False = it could not take it.
        self._answer = answer
        # R8: True while the plan opener waits for the group's answers.
        self._listening = listening or (lambda: False)
        self._writer = writer
        self._store = store
        self._service = service
        self._send_proposal = send_proposal
        # Who a card may name: the whole group when linked, else this channel.
        self._members = members or (lambda: dict(conversation.participants()))
        # R3 counts this channel's own messages; R4's time is the group's.
        self._count_mark = f"proactive:{getattr(conversation, 'conversation_id', '')}"
        self._today = today
        self._clock = clock
        self._task: asyncio.Task | None = None
        # R2: the strategy being drafted, and whether its draft is still only
        # a model call (it can then be replaced; once saving starts, never).
        self._drafting: str | None = None
        # Message count and time of the last card posted (R3, R4).
        self._last_count: int | None = None
        self._last_at: float | None = None
        self._mark_loaded = False
        # R10: texts the client sent to Cadence's ask, and when.
        self._claimed: dict[str, float] = {}
        # Card offers vetoed by R10, by message: (at, strategy, history, count, assistant id, reason).
        self._held: dict[str, tuple] = {}
        # Messages ask read as a possible follow-up and did not answer: a card may still come.
        self._silent: dict[str, float] = {}
        # K1: the project memory last told to the observer, and when.
        self._memory_sent: str | None = None
        self._memory_sent_at_count = 0

    # ------------------------------------------------------------------
    # Called by CoalaPolicy
    # ------------------------------------------------------------------

    async def offer(
        self,
        strategy_id: str,
        *,
        history: list,
        message_count: int,
        assistant_id: int,
        reason: str = "",
    ) -> Verdict:
        """
        Decide at once (the turn is waiting); the draft runs in the background.
        `reason`: the intervention agent's words on why the strategy fired
        (its prompt fragment), handed to the card writer.
        """
        if strategy_id not in STRATEGY_KINDS:
            if strategy_id not in SOLICITED:
                if self._group_text_turn(history):
                    logger.info(
                        "%s coordinator vetoed %s: R12 the lab's unsolicited strategies stay silent",
                        self._conversation.conversation_id, strategy_id,
                    )
                    return Verdict.VETOED
                return Verdict.NOT_MINE
            if not history:
                return Verdict.NOT_MINE
            last = history[-1]
            if self.is_claimed(last.content):
                logger.info("%s coordinator: R10 Cadence's ask already has this message", self._conversation.conversation_id)
                return Verdict.VETOED
            return self._hand_to_ask(strategy_id, last, assistant_id)
        if history and self.is_claimed(history[-1].content) and claim_key(history[-1].content) not in self._silent:
            logger.info(
                "%s coordinator vetoed %s: R10 Cadence's ask already has this message",
                self._conversation.conversation_id, strategy_id,
            )
            # Kept for a moment: if ask stays silent on this message (it was
            # not for Cadence after all), the card it deserved is offered
            # again (`release`). 22/11: "I'm stuck on the screen texts" and
            # "I'll have them by tomorrow" got nothing, because each was
            # written right after its author had talked to Cadence.
            self._held[claim_key(history[-1].content)] = (
                self._clock(), strategy_id, list(history), message_count, assistant_id, reason
            )
            return Verdict.VETOED
        await self._load_mark()
        if self._preempt(strategy_id):
            logger.info("%s coordinator: %s replaces a draft still being written", self._conversation.conversation_id, strategy_id)
        # Not `reason`: that is the judge's own words, passed on to the writer below.
        vetoed = self._veto(history, message_count, assistant_id, strategy_id)
        if vetoed:
            logger.info("%s coordinator vetoed %s: %s", self._conversation.conversation_id, strategy_id, vetoed)
            return Verdict.VETOED
        chat = [
            ChatLine(speaker=u.speaker_name or str(u.speaker_id), text=u.content, at_ms=u.timestamp_ms)
            for u in history[-CHAT_WINDOW:]
            if u.speaker_id != assistant_id and u.content.strip()
        ]
        self._task = asyncio.create_task(self._post(strategy_id, chat, message_count, reason))
        return Verdict.TAKEN

    def set_answer(self, answer: Callable[[int, str], bool] | None) -> None:
        """R11: the router gives the way to Cadence's ask once its commands exist."""
        self._answer = answer

    def _group_text_turn(self, history: list) -> bool:
        """True for a typed message in a group conversation (R11, R12 apply only there)."""
        if self._conversation.is_private or self._conversation.is_ephemeral:
            return False
        return not history or getattr(history[-1], "modality", "text") == "text"

    def _hand_to_ask(self, strategy_id: str, last: Any, assistant_id: int) -> Verdict:
        """R11: a solicited strategy in a group text chat is answered by Cadence's ask."""
        conversation_id = self._conversation.conversation_id
        text = (getattr(last, "content", "") or "").strip()
        speaker = getattr(last, "speaker_id", None)
        if (
            self._answer is None
            or self._conversation.is_private
            or self._conversation.is_ephemeral
            or getattr(last, "modality", "text") != "text"
            or not text
            or speaker is None
            or speaker == assistant_id
        ):
            return Verdict.NOT_MINE
        try:
            taken = self._answer(speaker, text)
        except Exception:  # the lab's path stays the fallback: the member still gets an answer
            logger.exception("%s coordinator: could not hand %s to Cadence's ask", conversation_id, strategy_id)
            return Verdict.NOT_MINE
        if not taken:
            return Verdict.NOT_MINE
        self.claim(text)
        logger.info("%s coordinator: R11 %s answered by Cadence's ask", conversation_id, strategy_id)
        return Verdict.TAKEN

    def claim(self, text: str) -> None:
        """R10: the client sent this message to Cadence's ask; the lab's agent must not answer it too."""
        now = self._clock()
        self._claimed = {k: at for k, at in self._claimed.items() if now - at < CLAIM_SECONDS}
        self._claimed[claim_key(text)] = now

    async def release(self, text: str) -> Verdict | None:
        """
        Cadence's ask read this message as a possible follow-up and stayed
        silent: the message is nobody's any more. If the judge had a card for
        it (held by R10), that card is offered now; if the judge comes later,
        it is let through. Returns the new verdict, or None if nothing was held.
        """
        key = claim_key(text)
        now = self._clock()
        # The claim stays (the lab's agent must still not answer this message);
        # only a card is let through from now on.
        self._silent = {k: at for k, at in self._silent.items() if now - at < CLAIM_SECONDS}
        self._silent[key] = now
        self._held = {k: v for k, v in self._held.items() if now - v[0] < CLAIM_SECONDS}
        held = self._held.pop(key, None)
        if held is None:
            return None
        _, strategy_id, history, message_count, assistant_id, reason = held
        logger.info("%s coordinator: ask stayed silent, %s is offered again", self._conversation.conversation_id, strategy_id)
        return await self.offer(strategy_id, history=history, message_count=message_count, assistant_id=assistant_id, reason=reason)

    def is_claimed(self, text: str) -> bool:
        at = self._claimed.get(claim_key(text))
        return at is not None and self._clock() - at < CLAIM_SECONDS

    def busy(self) -> str:
        """For time-driven messages (CS6, timed check-in): R2 and R8 of this bridge, or ""."""
        if self._task is not None and not self._task.done():
            return "R2 a card draft is running"
        if self._listening():
            return "R8 the plan opener is listening"
        return ""

    async def observer_note(self, message_count: int) -> str:
        """
        K1: the project memory for the intervention agent (project_memory.py),
        or "" when it has not changed since it was last sent and fewer than
        RESEND_EVERY messages have passed. Called by the policy on every
        judged turn; a database error gives "" (the turn goes on as before).
        """
        try:
            state = await self._store().load_state()
            note = project_memory(state, dict(self._members()), self._today())
        except Exception:
            logger.exception("%s coordinator: could not build the project memory", self._conversation.conversation_id)
            return ""
        if not note:
            return ""
        stale = message_count - self._memory_sent_at_count >= RESEND_EVERY
        if note == self._memory_sent and not stale:
            return ""
        self._memory_sent = note
        self._memory_sent_at_count = message_count
        return note

    async def wait(self) -> None:
        """Wait for a running draft (tests, shutdown)."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _load_mark(self) -> None:
        """
        The R3/R4 markers from the group's file. The time of the last card is
        read at every offer (another channel of the group may have posted one
        a minute ago); this channel's message count only once, after a restart.
        """
        try:
            store = self._store()
            shared = await store.mark("proactive")
            if shared is not None and (self._last_at is None or shared[1] > self._last_at):
                self._last_at = shared[1]
            if not self._mark_loaded:
                self._mark_loaded = True
                own = await store.mark(self._count_mark)
                if own is not None and self._last_count is None:
                    self._last_count = own[0]
        except Exception:
            logger.exception("%s coordinator: could not read the R3/R4 marker", self._conversation.conversation_id)

    # ------------------------------------------------------------------
    # Rules and drafting
    # ------------------------------------------------------------------

    def _preempt(self, strategy_id: str) -> bool:
        """R2: cancel a running draft of a less urgent strategy, if it is still only a model call."""
        running = self._drafting
        if self._task is None or self._task.done() or running is None:
            return False
        if PRIORITY.get(strategy_id, 0) <= PRIORITY.get(running, 0):
            return False
        self._task.cancel()
        self._task = None
        self._drafting = None
        return True

    def _veto(self, history: list, message_count: int, assistant_id: int, strategy_id: str = "") -> str:
        """The first rule (R1-R5, R8) that says no, or "" if none does."""
        if self._conversation.is_private or self._conversation.is_ephemeral:
            return "R1 not a group conversation"
        if self._task is not None and not self._task.done():
            return "R2 a draft is already running"
        if self._listening():
            return "R8 the plan opener is listening for the group's answers"
        stated = strategy_id in STATED_NEEDS
        messages = STATED_MESSAGES_BETWEEN if stated else MIN_MESSAGES_BETWEEN
        seconds = STATED_SECONDS_BETWEEN if stated else MIN_SECONDS_BETWEEN
        if self._last_count is not None and message_count - self._last_count < messages:
            return "R3 too few messages since the last card"
        if self._last_at is not None and self._clock() - self._last_at < seconds:
            return "R4 too little time since the last card"
        if len(history) >= 2 and history[-2].speaker_id == assistant_id:
            # history[-1] is the message being judged; the one before it is
            # the agent's own: a card now would be two agent messages in a row.
            return "R5 the agent spoke last"
        return ""

    async def _post(self, strategy_id: str, chat: list[ChatLine], message_count: int, noticed: str = "") -> None:
        """Draft, check R6 and R7, save the pending proposal, send the card."""
        conversation_id = self._conversation.conversation_id
        try:
            store = self._store()
            state = await store.load_state()
            snoozes = await store.active_snoozes(self._clock())
            silenced = sorted(topic for sid, topic in snoozes if sid == strategy_id)
            members = dict(self._members())
            self._drafting = strategy_id
            draft = await self._writer.draft(
                strategy_id,
                chat=chat,
                members=members,
                profiles=state.members,
                tasks=state.tasks,
                silenced_topics=silenced,
                today=self._today(),
                decisions=state.decisions,
                noticed=noticed,
            )
            self._drafting = None  # from here on the draft is saved or dropped, never replaced
            if draft is None:
                logger.warning("%s coordinator: no valid %s draft, staying silent", conversation_id, strategy_id)
                return
            dropped = _dropped(draft, silenced, state.tasks, state.decisions) or _duplicate(
                draft, await store.open_proposals(self._clock())
            )
            if dropped:
                logger.info("%s coordinator dropped %s: %s", conversation_id, strategy_id, dropped)
                return
            proposal = await self._service().create(
                draft.kind,
                draft.payload,
                rationale=draft.rationale,
                origin_channel="group",
            )
            self._last_count = message_count
            self._last_at = self._clock()
            await store.save_mark("proactive", self._last_count, self._last_at)
            await store.save_mark(self._count_mark, self._last_count, self._last_at)
            await self._send_proposal(proposal)
        except asyncio.CancelledError:
            logger.info("%s coordinator: %s draft replaced by a more urgent one", conversation_id, strategy_id)
            raise
        except Exception:
            # Project rule: a model or database error never breaks the chat.
            logger.exception("%s coordinator failed on %s", conversation_id, strategy_id)
        finally:
            if self._drafting == strategy_id:
                self._drafting = None


def _duplicate(draft: ProactiveDraft, open_cards: list[Proposal]) -> str:
    """
    R9: a card still waiting for a click already asks the same thing (same
    kind and topic, or for CS5/CS2 the same task title). The group can still
    answer the first one; a second copy would only be noise.
    """
    topic = topic_key(draft.payload.context.topic)
    title = topic_key(getattr(draft.payload, "title", "") or "")
    for card in open_cards:
        if card.kind is not draft.kind:
            continue
        context = card.payload.get("context") or {}
        if topic_key(context.get("topic", "")) == topic:
            return "R9 an open card already asks this"
        if title and topic_key(card.payload.get("title", "") or "") == title:
            return "R9 an open card already asks this"
    return ""


def _dropped(draft: ProactiveDraft, silenced: list[str], tasks, decisions=()) -> str:
    """R6 and R7, on the finished draft. Returns why it is dropped, or ""."""
    context = draft.payload.context
    if topic_key(context.topic) in silenced:
        return "R6 topic silenced with Not now"
    if draft.kind is ProposalKind.TASK_OWNER:
        assert isinstance(draft.payload, TaskAssignmentPayload)
        title = topic_key(draft.payload.title)
        if any(t.owner_id is not None and topic_key(t.title) == title for t in tasks):
            return "R7 the plan already has this task with an owner"
    if draft.kind is ProposalKind.BLOCKER:
        assert isinstance(draft.payload, BlockerPayload)
        payload = draft.payload
        task = next((t for t in tasks if payload.task_id is not None and t.task_id == payload.task_id), None)
        if payload.certainty is BlockerCertainty.BLOCKED and task is not None and task.status is TaskStatus.BLOCKED:
            return "R7 the task is already marked blocked"
        if task is not None and payload.depends_on_task_id is not None and payload.depends_on_task_id in task.depends_on:
            return "R7 the plan already records this dependency"
        if payload.depends_on_task_id is None and known_prerequisite(
            task, payload.depends_on_title, payload.depends_on_owner_id, tasks
        ):
            return "R7 the plan already records this prerequisite"
    if draft.kind is ProposalKind.REVISIT:
        decision = next((d for d in decisions if d.decision_id == draft.payload.decision_id), None)
        if decision is None or decision.reopened_at is not None:
            return "R7 the decision is no longer closed in the log"
    return ""
