"""
PlanCommands: the M4 commands of one group session, used by router.py.

    planning  args.text = the tasks the members wrote, one per line
              -> PlanWriter suggests owners and dates -> one "plan" card
    checkin   no args -> checkin.findings() in code; if nothing is late or at
              risk, a notice "on track"; else PlanWriter proposes up to three
              fixes -> one "checkin" card (findings + options)
    task      args.task = task id or its number in the plan, args.status
              -> a member updates a task (their own action: no card, no model)
    why       args.question -> PlanWriter answers from the decision log only,
              with the decisions it rests on as the source

The check-in also runs on time (spec FR15): checkin_timer.py calls
`timed_checkin`, which posts a card only when something is late or at risk.

planning, checkin and why call the model, so they run in the background (like
the kickoff) and only one runs at a time per group: the session reads its
messages one by one and must never wait for the model.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from .checkin import findings, owners_report
from .models import ProposalKind, TaskStatus
from .plan_writer import PlanWriter, WhyAnswer
from .proposals import ProposalService
from .store import CoordinationStore

logger = logging.getLogger(__name__)

BUSY = "The coordinator is already working on a request for this group: its answer will appear here."
MAX_TASKS_TEXT = 4000
MAX_QUESTION = 500


class PlanCommands:
    def __init__(
        self,
        *,
        participants: Callable[[], dict[int, str]],
        store: Callable[[], CoordinationStore],
        service: Callable[[], ProposalService],
        writer: PlanWriter,
        send_proposal: Callable[[Any], Awaitable[None]],
        notice: Callable[[bool, str, str], Awaitable[None]],
        send_state: Callable[[str], Awaitable[None]],
        unavailable: Callable[[str, str], Awaitable[None]],
        today: Callable[[], date] = date.today,
    ) -> None:
        self._participants = participants
        self._store = store
        self._service = service
        self._writer = writer
        self._send_proposal = send_proposal
        self._notice = notice
        self._send_state = send_state
        self._unavailable = unavailable
        self._today = today
        self._task: asyncio.Task | None = None

    async def handle(self, message) -> None:
        """Entry point for planning, checkin, task and why (router.py checked membership)."""
        if message.command == "task":
            await self._on_task(message)
            return
        handlers = {"planning": self._planning, "checkin": self._checkin, "why": self._why}
        if self._task is not None and not self._task.done():
            await self._notice(False, BUSY, message.request_id)
            return
        self._task = asyncio.create_task(self._run(handlers[message.command], message))

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self, handler, message) -> None:
        try:
            await handler(message)
        except Exception:
            await self._unavailable(message.event, message.request_id)

    # ------------------------------------------------------------------

    async def _planning(self, message) -> None:
        text = str(message.args.get("text", "")).strip()[:MAX_TASKS_TEXT]
        if not text:
            await self._notice(False, "Write the tasks, one per line, then send them.", message.request_id)
            return
        state = await self._store().load_state()
        draft = await self._writer.draft_plan(text, members=self._participants(), state=state, today=self._today())
        if draft is None:
            await self._unavailable(message.event, message.request_id)
            return
        proposal = await self._service().create(
            ProposalKind.PLAN, draft.payload, rationale=draft.rationale, origin_channel=message.channel
        )
        await self._send_proposal(proposal)
        await self._notice(True, "Plan card posted: any member can confirm or reject it.", message.request_id)

    async def _checkin(self, message) -> None:
        await self.run_checkin(message.channel, message.request_id, quiet=False)

    async def run_checkin(self, channel: str, request_id: str, *, quiet: bool) -> bool:
        """
        The check-in, on command or on time. True when a card was posted.
        `quiet` (the timed one): no notice when on track or when the model
        fails, because nobody asked and the chat must not fill with notices.
        """
        members = self._participants()
        state = await self._store().load_state()
        found = findings(state, members, self._today())
        report = owners_report(state, members)
        status = ("Open tasks — " + "; ".join(report)) if report else "Nobody has open tasks."
        if not found:
            if not quiet:
                await self._notice(True, f"Check-in: on track, nothing late or at risk. {status}", request_id)
            return False
        draft = await self._writer.draft_checkin(found, members=members, state=state, today=self._today())
        if draft is None:
            if not quiet:
                await self._unavailable("coordinator_command", request_id)
            return False
        proposal = await self._service().create(
            ProposalKind.CHECKIN, draft.payload, rationale=draft.rationale, origin_channel=channel
        )
        await self._send_proposal(proposal)
        if not quiet:
            await self._notice(True, f"Check-in card posted. {status}", request_id)
        return True

    async def timed_checkin(self) -> bool:
        """Called by checkin_timer.py; skipped while a member's command is running."""
        if self._task is not None and not self._task.done():
            return False
        return await self.run_checkin("group", "", quiet=True)

    async def _why(self, message) -> None:
        question = str(message.args.get("question", "")).strip()[:MAX_QUESTION]
        if not question:
            await self._notice(False, "Write the question about the decision.", message.request_id)
            return
        state = await self._store().load_state()
        if not state.decisions:
            await self._notice(True, "No decision has been recorded yet.", message.request_id)
            return
        answer = await self._writer.answer_why(question, decisions=state.decisions)
        if answer is None:
            await self._unavailable(message.event, message.request_id)
            return
        await self._notice(True, why_text(answer, self._participants()), message.request_id)

    async def _on_task(self, message) -> None:
        """A member updates a task's status. Their own action: no card, no model."""
        status = str(message.args.get("status", "")).strip().lower()
        if status not in {s.value for s in TaskStatus}:
            await self._notice(False, "Status must be todo, doing, done or blocked.", message.request_id)
            return
        tasks = (await self._store().load_state()).tasks
        ref = str(message.args.get("task", "")).strip()
        task = next((t for t in tasks if t.task_id == ref), None)
        if task is None and ref.isdigit() and 1 <= int(ref) <= len(tasks):
            task = tasks[int(ref) - 1]  # the number shown in the plan
        if task is None:
            await self._notice(False, "No such task in the plan.", message.request_id)
            return
        await self._store().set_task_status(task.task_id, status)
        await self._notice(True, f"“{task.title}” is now {status}.", message.request_id)
        await self._send_state("")


def why_text(answer: WhyAnswer, members: dict[int, str]) -> str:
    """The answer, then its source: each decision with who decided, when, and from which card."""
    lines = [answer.answer]
    for d in answer.decisions:
        when = datetime.fromtimestamp(d.decided_at).strftime("%d %b")
        who = members.get(d.decided_by, "a member")
        source = f", card {d.source.split(':', 1)[1]}" if d.source.startswith("proposal:") else ""
        lines.append(f"Source: “{d.question}” → {d.choice} (decided by {who} on {when}{source}).")
    return "\n".join(lines)
