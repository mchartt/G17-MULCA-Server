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
    R2  one draft at a time per group
    R3  at least MIN_MESSAGES_BETWEEN messages since the last proactive card
    R4  at least MIN_SECONDS_BETWEEN since the last proactive card
    R5  never right after the agent's own message (no two in a row)
    R6  "Not now": a silenced (strategy, topic) never gets a card (checked
        after the draft, when the topic is known)
    R7  CS5: no card for a task the plan already shows with an owner;
        CS7: no card for a task already marked blocked

R3 and R4 count only cards actually posted: a draft that fails or is
dropped does not silence the group. Their marker is saved in the group's
SQLite file (store.save_mark("proactive", ...)) and read back on the first offer of
a new session, so a server restart does not reset them. The policy's own cooldown
(`cooldown_messages` in the strategy YAML) still applies on top.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from enum import StrEnum
from typing import Any

from .models import BlockerPayload, Proposal, ProposalKind, TaskAssignmentPayload, TaskStatus
from .plan_writes import topic_key
from .proactive_writer import STRATEGY_KINDS, ChatLine, ProactiveDraft, ProactiveWriter
from .proposals import ProposalService
from .store import CoordinationStore

logger = logging.getLogger(__name__)

MIN_MESSAGES_BETWEEN = 10  # NFR3: one unsolicited intervention every 10 messages at most
MIN_SECONDS_BETWEEN = 5 * 60  # NFR3: ... and every 5 minutes at most
CHAT_WINDOW = 20  # recent messages the writer sees


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
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._conversation = conversation
        self._writer = writer
        self._store = store
        self._service = service
        self._send_proposal = send_proposal
        self._today = today
        self._clock = clock
        self._task: asyncio.Task | None = None
        # Message count and time of the last card posted (R3, R4).
        self._last_count: int | None = None
        self._last_at: float | None = None
        self._mark_loaded = False

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
    ) -> Verdict:
        """Decide at once (the turn is waiting); the draft runs in the background."""
        if strategy_id not in STRATEGY_KINDS:
            return Verdict.NOT_MINE
        await self._load_mark()
        reason = self._veto(history, message_count, assistant_id)
        if reason:
            logger.info("%s coordinator vetoed %s: %s", self._conversation.conversation_id, strategy_id, reason)
            return Verdict.VETOED
        chat = [
            ChatLine(speaker=u.speaker_name or str(u.speaker_id), text=u.content, at_ms=u.timestamp_ms)
            for u in history[-CHAT_WINDOW:]
            if u.speaker_id != assistant_id and u.content.strip()
        ]
        self._task = asyncio.create_task(self._post(strategy_id, chat, message_count))
        return Verdict.TAKEN

    async def wait(self) -> None:
        """Wait for a running draft (tests, shutdown)."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _load_mark(self) -> None:
        """Once per session: the R3/R4 marker saved before a restart."""
        if self._mark_loaded:
            return
        self._mark_loaded = True
        try:
            mark = await self._store().mark("proactive")
        except Exception:
            logger.exception("%s coordinator: could not read the R3/R4 marker", self._conversation.conversation_id)
            return
        if mark is not None and self._last_count is None:
            self._last_count, self._last_at = mark

    # ------------------------------------------------------------------
    # Rules and drafting
    # ------------------------------------------------------------------

    def _veto(self, history: list, message_count: int, assistant_id: int) -> str:
        """The first rule (R1-R5) that says no, or "" if none does."""
        if self._conversation.is_private or self._conversation.is_ephemeral:
            return "R1 not a group conversation"
        if self._task is not None and not self._task.done():
            return "R2 a draft is already running"
        if self._last_count is not None and message_count - self._last_count < MIN_MESSAGES_BETWEEN:
            return "R3 too few messages since the last card"
        if self._last_at is not None and self._clock() - self._last_at < MIN_SECONDS_BETWEEN:
            return "R4 too little time since the last card"
        if len(history) >= 2 and history[-2].speaker_id == assistant_id:
            # history[-1] is the message being judged; the one before it is
            # the agent's own: a card now would be two agent messages in a row.
            return "R5 the agent spoke last"
        return ""

    async def _post(self, strategy_id: str, chat: list[ChatLine], message_count: int) -> None:
        """Draft, check R6 and R7, save the pending proposal, send the card."""
        conversation_id = self._conversation.conversation_id
        try:
            store = self._store()
            state = await store.load_state()
            snoozes = await store.active_snoozes(self._clock())
            silenced = sorted(topic for sid, topic in snoozes if sid == strategy_id)
            members = dict(self._conversation.participants())
            draft = await self._writer.draft(
                strategy_id,
                chat=chat,
                members=members,
                profiles=state.members,
                tasks=state.tasks,
                silenced_topics=silenced,
                today=self._today(),
            )
            if draft is None:
                logger.warning("%s coordinator: no valid %s draft, staying silent", conversation_id, strategy_id)
                return
            dropped = _dropped(draft, silenced, state.tasks)
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
            await self._send_proposal(proposal)
        except Exception:
            # Project rule: a model or database error never breaks the chat.
            logger.exception("%s coordinator failed on %s", conversation_id, strategy_id)


def _dropped(draft: ProactiveDraft, silenced: list[str], tasks) -> str:
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
        if any(t.task_id == draft.payload.task_id and t.status is TaskStatus.BLOCKED for t in tasks):
            return "R7 the task is already marked blocked"
    return ""
