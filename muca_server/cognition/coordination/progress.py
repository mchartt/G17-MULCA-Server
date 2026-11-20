"""
CS6 progress check-ins: when a short question to a task's owner could help.

Not a reminder timer. Every few minutes (checkin_timer.py) the coordinator
re-reads the plan and asks itself, in code, whether one question about one
task's progress is worth sending now. Most of the time the answer is no.

    candidates(...)  every task worth an assessment, already filtered and scored
    pick(...)        at most ONE of them, or None
    ProgressMonitor  the sweep: shared budget, claim, wording (model), post

Conventions (documented in docs/coordinator/DECISIONS.md, D-CS6):
    deadline       a task's due_date is a date with no time: it means the END
                   of that day (23:59:59) in the project's time zone
                   (MUCA_COORDINATOR_TIMEZONE, default Europe/Rome; zoneinfo
                   handles daylight saving)
    window         the assessment opens ASSESS_HOURS before that deadline
                   (36 h: from noon the day before, so "la consegna è domani"
                   can be asked in daytime, about 24 h ahead) and stays open
                   until OVERDUE_HOURS after it (one gentle question about a
                   task just overdue; never repeated pressure)
    one per key    a task is asked about at most once per due date
                   (store.claim_progress_checkin); a new due date is a new
                   key, so a question planned for the old date never fires

Filtered out (no question): done; no owner (that gap is CS5's); no date;
already asked or answered for this due date; status or progress changed by a
member in the last CONFIRMED_HOURS; the task silenced with "Not now" (snooze,
strategy cs6); the task already discussed in the chat in the last
CHAT_HOURS (someone is giving its status: asking again would be noise).

Scored (deterministic, from the plan only, never from who talks less):
closeness to the deadline, open tasks that wait on it (downstream impact),
blocked status, no progress recorded at all. Silence is never a delay: a
member who has not written is not behind; the question only asks.

Shared budget (budget(), with the proactive bridge's own rules): never in a
DM, never while a card draft runs or the plan opener listens, never in quiet
hours, at most one time-driven question per SPACING_SECONDS for the group,
never within MIN_SECONDS_AFTER_CARD of any proactive card (it reads the
cards' marker; a question does not move it, so a CS7 card answering the
owner's reply is not blocked), never two questions to the
same member within MEMBER_SECONDS, never right after Cadence's own message.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from .models import ProjectState, Task, TaskStatus
from .plan_writes import topic_key

logger = logging.getLogger(__name__)

STRATEGY_ID = "cs6_progress_checkin"  # snoozes ("Not now", "not today") use this id
ASSESS_HOURS = 36
OVERDUE_HOURS = 24
CONFIRMED_HOURS = 48
CHAT_HOURS = 12
SPACING_SECONDS = 4 * 3600  # one time-driven question per group every 4 hours at most
MIN_SECONDS_AFTER_CARD = 30 * 60  # and never within 30 minutes of any proactive card
MEMBER_SECONDS = 24 * 3600  # never two questions to the same member within a day
QUIET_HOURS = (22, 8)  # local time: no unsolicited question from 22:00 to 08:00
MARK = "progress"  # store mark: time of the last check-in question sent
DEFAULT_TIMEZONE = "Europe/Rome"
ANSWERS = frozenset({"on_track", "behind", "blocked", "done", "declined"})

_STOP = {"the", "and", "for", "with", "della", "delle", "degli", "dello", "per", "con", "una", "first", "prima"}


@dataclass(frozen=True, slots=True)
class Candidate:
    task: Task
    hours_left: float  # negative when overdue
    downstream: tuple[Task, ...]  # open tasks that wait on it
    score: float

    @property
    def due_key(self) -> str:
        return self.task.due_date.isoformat() if self.task.due_date else ""


def deadline(due: date, tz: ZoneInfo) -> datetime:
    """The convention: a date-only deadline is the end of that day in the project's time zone."""
    return datetime.combine(due, clock_time(23, 59, 59), tzinfo=tz)


def words(title: str) -> set[str]:
    """The words of a title that identify it in a chat line (4+ letters, no filler)."""
    return {w for w in re.findall(r"[\w']+", title.lower()) if len(w) >= 4 and w not in _STOP}


def discussed(task: Task, chat: list[tuple[float, str]], since: float) -> bool:
    """The task was talked about in the chat after `since`: its title's words appear in a line."""
    title = words(task.title)
    if not title:
        return False
    need = 1 if len(title) == 1 else 2
    for at, line in chat:
        if at >= since and len(title & set(re.findall(r"[\w']+", line.lower()))) >= need:
            return True
    return False


def candidates(
    state: ProjectState,
    ledger: list[dict],
    *,
    now: float,
    tz: ZoneInfo,
    snoozed: set[str] = frozenset(),
    chat: list[tuple[float, str]] = (),
) -> list[Candidate]:
    """Every task worth an assessment now, best first. Pure: no clock, no I/O."""
    seen = {(row["task_id"], row["due_date"]) for row in ledger if row.get("asked_at") or row.get("answer")}
    found: list[Candidate] = []
    for task in state.tasks:
        if task.status is TaskStatus.DONE or task.due_date is None or task.owner_id is None:
            continue
        hours_left = (deadline(task.due_date, tz).timestamp() - now) / 3600
        if hours_left > ASSESS_HOURS or hours_left < -OVERDUE_HOURS:
            continue
        if (task.task_id, task.due_date.isoformat()) in seen:
            continue
        if task.touched_at is not None and now - task.touched_at < CONFIRMED_HOURS * 3600 and task.status is not TaskStatus.BLOCKED:
            continue
        if topic_key(task.title) in snoozed or task.task_id in snoozed:
            continue
        if discussed(task, list(chat), now - CHAT_HOURS * 3600):
            continue
        downstream = tuple(t for t in state.tasks if task.task_id in t.depends_on and t.status is not TaskStatus.DONE)
        score = (
            (ASSESS_HOURS - max(hours_left, 0)) / ASSESS_HOURS * 3  # closer deadline, up to 3
            + 2 * len(downstream)
            + (3 if task.status is TaskStatus.BLOCKED else 0)
            + (1 if task.progress == 0 else 0)
            - (1 if hours_left < 0 else 0)  # overdue: still worth one question, but no more urgent than a live one
        )
        found.append(Candidate(task, hours_left, downstream, score))
    found.sort(key=lambda c: (-c.score, c.hours_left, c.task.task_id))
    return found


def pick(found: list[Candidate], ledger: list[dict], *, now: float) -> Candidate | None:
    """At most one question: the best candidate whose owner was not asked in the last day."""
    asked_recently = {
        row["member_id"] for row in ledger if row.get("asked_at") and now - row["asked_at"] < MEMBER_SECONDS
    }
    for candidate in found:
        if candidate.task.owner_id not in asked_recently:
            return candidate
    return None


def quiet(now: float, tz: ZoneInfo) -> bool:
    start, end = QUIET_HOURS
    hour = datetime.fromtimestamp(now, tz).hour
    return hour >= start or hour < end


def timezone_from(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        logger.warning("unknown time zone %r, using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


class ProgressMonitor:
    """
    One sweep = one assessment of the whole plan; it sends at most one short
    question. Called by checkin_timer.py; every dependency is injected.
    """

    def __init__(
        self,
        conversation: Any,
        *,
        store: Callable[[], Any],
        members: Callable[[], dict[int, str]],
        write: Callable[..., Awaitable[str | None]],
        say: Callable[[str], Awaitable[None]],
        chat: Callable[[], Awaitable[list[tuple[float, str]]]],
        busy: Callable[[], str] = lambda: "",
        open_card_titles: Callable[[], Awaitable[set[str]]] | None = None,
        tz: ZoneInfo | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._conversation = conversation
        self._store = store
        self._members = members
        self._write = write
        self._say = say
        self._chat = chat
        self._busy = busy
        self._open_card_titles = open_card_titles
        self._tz = tz or ZoneInfo(DEFAULT_TIMEZONE)
        self._clock = clock

    async def budget(self, store: Any, now: float, chat: list[tuple[float, str]]) -> str:
        """The first shared rule that says "not now", or "" (see the module docstring)."""
        conversation = self._conversation
        if conversation.is_private or conversation.is_ephemeral or not conversation.conversation_id:
            return "not a group conversation"
        busy = self._busy()
        if busy:
            return busy
        if quiet(now, self._tz):
            return "quiet hours"
        mine = await store.mark(MARK)
        if mine is not None and now - mine[1] < SPACING_SECONDS:
            return "a check-in question was sent less than SPACING_SECONDS ago"
        card = await store.mark("proactive")
        if card is not None and now - card[1] < MIN_SECONDS_AFTER_CARD:
            return "a proactive card was posted a short while ago"
        if chat and chat[-1][1].startswith("Cadence:"):
            return "Cadence spoke last"
        return ""

    async def sweep(self) -> bool:
        """One assessment. True when a question was sent."""
        store = self._store()
        now = self._clock()
        chat = await self._chat()
        reason = await self.budget(store, now, chat)
        if reason:
            logger.debug("%s progress check-in: not now (%s)", self._conversation.conversation_id, reason)
            return False
        state = await store.load_state()
        ledger = await store.progress_checkins()
        snoozed = {topic for sid, topic in await store.active_snoozes(now) if sid == STRATEGY_ID}
        found = candidates(state, ledger, now=now, tz=self._tz, snoozed=snoozed, chat=chat)
        if self._open_card_titles is not None and found:
            # A card about the task is already waiting for a click: that card is the conversation.
            open_titles = await self._open_card_titles()
            found = [c for c in found if topic_key(c.task.title) not in open_titles]
        choice = pick(found, ledger, now=now)
        if choice is None:
            return False
        # Claimed before any model call: a second timer, worker or a restart
        # in the middle never asks the same thing twice.
        if not await store.claim_progress_checkin(choice.task.task_id, choice.due_key, choice.task.owner_id, now):
            return False
        members = self._members()
        text = await self._write(choice, members=members, state=state, chat=[line for _, line in chat], now=now, tz=self._tz)
        if not text:
            # The model judged the question useless (status already given in
            # the chat) or failed: silence, and this deadline is not asked again.
            await store.record_progress(choice.task.task_id, choice.due_key, "skipped", now)
            return False
        # Only CS6's own marker: the cards' R4 marker is NOT moved, so the
        # owner's reply ("sto aspettando il dataset, sono fermo") can still
        # get its CS7 card right away. The burst to prevent is card ->
        # unrelated question (MIN_SECONDS_AFTER_CARD), not question -> the
        # card that answers it.
        await store.save_mark(MARK, 0, now)
        await self._say(text)
        logger.info(
            "%s progress check-in on %s (%.0f h left, %d waiting on it)",
            self._conversation.conversation_id, choice.task.task_id, choice.hours_left, len(choice.downstream),
        )
        return True
