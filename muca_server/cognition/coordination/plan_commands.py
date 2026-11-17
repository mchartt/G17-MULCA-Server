"""
PlanCommands: the M4 commands of one group session, used by router.py.

    planning  args.text = the tasks the members wrote, one per line; or
              args.items = [{title, owner_id?, due_date?}] (a list with the
              owners the members already chose)
              -> PlanWriter suggests owners and dates for the tasks without
              an owner -> one "plan" card. All owners chosen: no model call.
    checkin   no args -> checkin.findings() in code; if nothing is late or at
              risk, a notice "on track"; else PlanWriter proposes up to three
              fixes -> one "checkin" card (findings + options)
    task      args.task = task id or its number in the plan, with any of
              args.status, args.owner, args.due, args.title, args.remove;
              or args.add = the title of a new task (+ owner, due)
              -> a member edits the plan by hand (their own action: no card, no model)
    milestone args.add = title (+ due); or args.milestone = its id with
              args.title, args.due or args.remove -> by hand, like task
    why       args.question -> PlanWriter answers from the decision log only,
              with the decisions it rests on as the source
    ask       args.question -> PlanWriter answers from the saved tasks,
              milestones and decisions, each named as the source; when none
              of them answers, the server says so (spec: "@coordinator")

The check-in also runs on time (spec FR15): checkin_timer.py calls
`timed_checkin`, which posts a card only when something is late or at risk.

planning, checkin, why and ask call the model, so they run in the background (like
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
from pydantic import ValidationError

from .models import MAX_TITLE, Decision, PlanItem, PlanPayload, ProposalKind, TaskStatus
from .plan_writer import AskAnswer, PlanWriter, WhyAnswer
from .proposals import ProposalService, new_id
from .store import CoordinationStore

logger = logging.getLogger(__name__)

BUSY = "The coordinator is already working on a request for this group: its answer will appear here."
MAX_TASKS_TEXT = 4000
MAX_QUESTION = 500
NOT_SAVED = (
    "Nothing saved answers this. I can only read the plan, the milestones and the decisions the group confirmed."
)


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
        """Entry point for planning, checkin, task, why and ask (router.py checked membership)."""
        if message.command == "task":
            await self._on_task(message)
            return
        if message.command == "milestone":
            await self._on_milestone(message)
            return
        handlers = {"planning": self._planning, "checkin": self._checkin, "why": self._why, "ask": self._ask}
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
        members = self._participants()
        chosen: list[PlanItem] = []  # tasks whose owner the members already chose
        text = str(message.args.get("text", "")).strip()[:MAX_TASKS_TEXT]
        items = message.args.get("items")
        if isinstance(items, list) and items:
            open_titles = []
            for raw in items[:15]:
                if not isinstance(raw, dict):
                    continue
                title = " ".join(str(raw.get("title", "")).split())[:MAX_TITLE]
                if not title:
                    continue
                owner = _member_id(raw.get("owner_id"), members)
                due = _date(raw.get("due_date"))
                if owner is _INVALID or due is _INVALID:
                    await self._notice(False, f"Check “{title}”: its owner must be a member and its date a real day.", message.request_id)
                    return
                if due is not None and due < self._today():
                    await self._notice(False, f"The date of “{title}” is in the past.", message.request_id)
                    return
                if owner is None:
                    open_titles.append(title if due is None else f"{title} (by {due.isoformat()})")
                else:
                    chosen.append(PlanItem(title=title, owner_id=owner, reason="Chosen by the group.", due_date=due))
            text = "\n".join(open_titles)
        if not text and not chosen:
            await self._notice(False, "Write the tasks, one per line, then send them.", message.request_id)
            return
        if text:
            state = await self._store().load_state()
            draft = await self._writer.draft_plan(text, members=members, state=state, today=self._today())
            if draft is None:
                await self._unavailable(message.event, message.request_id)
                return
            payload = PlanPayload(items=[*chosen, *draft.payload.items][:15])
            rationale = draft.rationale
        else:  # every owner chosen by the members: nothing to suggest, no model call
            payload = PlanPayload(items=chosen)
            rationale = f"{members.get(message.member_id, 'A member')} listed the tasks with their owners."
        proposal = await self._service().create(
            ProposalKind.PLAN, payload, rationale=rationale, origin_channel=message.channel
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

    async def _ask(self, message) -> None:
        question = str(message.args.get("question", "")).strip()[:MAX_QUESTION]
        if not question:
            await self._notice(False, "Write your question after @coordinator.", message.request_id)
            return
        state = await self._store().load_state()
        if not (state.tasks or state.milestones or state.decisions):
            await self._notice(True, NOT_SAVED, message.request_id)  # nothing to read: no model call
            return
        answer = await self._writer.answer_question(question, members=self._participants(), state=state)
        if answer is None:
            await self._unavailable(message.event, message.request_id)
            return
        await self._notice(True, ask_text(answer, self._participants()), message.request_id)

    async def _on_task(self, message) -> None:
        """
        A member edits the plan by hand: a new task, or the status, owner,
        date or title of one, or its removal. Their own action: no card, no
        model. Every value is checked here (the owner is a member, the date
        is a real day), exactly as for a card.
        """
        args = message.args
        members = self._participants()
        fields: dict = {}
        if "status" in args:
            status = str(args.get("status", "")).strip().lower()
            if status not in {s.value for s in TaskStatus}:
                await self._notice(False, "Status must be todo, doing, done or blocked.", message.request_id)
                return
            fields["status"] = status
        if "owner" in args:
            owner = _member_id(args.get("owner"), members)
            if owner is _INVALID:
                await self._notice(False, "The owner must be a member of this group.", message.request_id)
                return
            fields["owner_id"] = owner
        if "due" in args:
            due = _date(args.get("due"))
            if due is _INVALID:
                await self._notice(False, "Write the date as YYYY-MM-DD.", message.request_id)
                return
            fields["due_date"] = due.isoformat() if due else None
        if "title" in args or "add" in args:
            title = " ".join(str(args.get("add") or args.get("title") or "").split())
            if not title or len(title) > MAX_TITLE:
                await self._notice(False, f"Write the task in a few words (up to {MAX_TITLE} characters).", message.request_id)
                return
            fields["title"] = title

        if "add" in args:
            await self._store().add_task_by_hand(new_id(), fields["title"], fields.get("owner_id"), fields.get("due_date"))
            owner_name = members.get(fields.get("owner_id"), "nobody yet")
            await self._notice(True, f"Task added: “{fields['title']}” ({owner_name}).", message.request_id)
            await self._send_state("")
            return

        tasks = (await self._store().load_state()).tasks
        ref = str(args.get("task", "")).strip()
        task = next((t for t in tasks if t.task_id == ref), None)
        if task is None and ref.isdigit() and 1 <= int(ref) <= len(tasks):
            task = tasks[int(ref) - 1]  # the number shown in the plan
        if task is None:
            await self._notice(False, "No such task in the plan.", message.request_id)
            return
        if args.get("remove") is True:
            await self._store().remove_task(task.task_id)
            await self._notice(True, f"“{task.title}” was removed from the plan.", message.request_id)
            await self._send_state("")
            return
        if not fields:
            await self._notice(False, "Say what to change: status, owner, date or title.", message.request_id)
            return
        await self._store().edit_task(task.task_id, fields)
        changes = []
        if "status" in fields:
            changes.append(f"is now {fields['status']}")
        if "owner_id" in fields:
            changes.append(f"is {members[fields['owner_id']]}'s" if fields["owner_id"] is not None else "has no owner")
        if "due_date" in fields:
            changes.append(f"is due {fields['due_date']}" if fields["due_date"] else "has no date")
        if "title" in fields:
            changes.append("was renamed")
        await self._notice(True, f"“{fields.get('title', task.title)}” {', '.join(changes)}.", message.request_id)
        await self._send_state("")

    async def _on_milestone(self, message) -> None:
        """A member adds, changes or removes a milestone by hand. No card, no model."""
        args = message.args
        fields: dict = {}
        if "due" in args:
            due = _date(args.get("due"))
            if due is _INVALID:
                await self._notice(False, "Write the date as YYYY-MM-DD.", message.request_id)
                return
            fields["due_date"] = due.isoformat() if due else None
        if "title" in args or "add" in args:
            title = " ".join(str(args.get("add") or args.get("title") or "").split())
            if not title or len(title) > MAX_TITLE:
                await self._notice(False, f"Name the milestone in a few words (up to {MAX_TITLE} characters).", message.request_id)
                return
            fields["title"] = title
        if "add" in args:
            await self._store().add_milestone(new_id(), fields["title"], fields.get("due_date"))
            await self._notice(True, f"Milestone added: “{fields['title']}”.", message.request_id)
            await self._send_state("")
            return
        milestones = (await self._store().load_state()).milestones
        ref = str(args.get("milestone", "")).strip()
        milestone = next((m for m in milestones if m.milestone_id == ref), None)
        if milestone is None:
            await self._notice(False, "No such milestone.", message.request_id)
            return
        if args.get("remove") is True:
            await self._store().remove_milestone(milestone.milestone_id)
            await self._notice(True, f"Milestone “{milestone.title}” was removed.", message.request_id)
        elif fields:
            await self._store().edit_milestone(milestone.milestone_id, fields)
            await self._notice(True, f"Milestone “{fields.get('title', milestone.title)}” was updated.", message.request_id)
        else:
            await self._notice(False, "Say what to change: the name or the date.", message.request_id)
            return
        await self._send_state("")


_INVALID = object()


def _member_id(value, members: dict[int, str]):
    """A member's id, None for "nobody" (None, "", "none"), or _INVALID."""
    if value is None or str(value).strip().lower() in ("", "none", "null"):
        return None
    try:
        member_id = int(str(value).strip())
    except ValueError:
        return _INVALID
    return member_id if member_id in members else _INVALID


def _date(value):
    """A date from YYYY-MM-DD, None for "no date" (None, ""), or _INVALID."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return _INVALID


def why_text(answer: WhyAnswer, members: dict[int, str]) -> str:
    """The answer, then its source: each decision with who decided, when, and from which card."""
    return "\n".join([answer.answer, *(_decision_source(d, members) for d in answer.decisions)])


def ask_text(answer: AskAnswer, members: dict[int, str]) -> str:
    """The answer, then every saved item it rests on; with no source, the fixed "nothing saved" line."""
    if not answer.found:
        return NOT_SAVED
    lines = [answer.answer]
    for t in answer.tasks:
        owner = members.get(t.owner_id, "a member") if t.owner_id else "no owner"
        due = t.due_date.strftime("%d %b") if t.due_date else "no date"
        lines.append(f"Source: task “{t.title}” ({owner}, due {due}, {t.status.value}).")
    for m in answer.milestones:
        lines.append(f"Source: milestone “{m.title}” ({m.due_date.strftime('%d %b') if m.due_date else 'no date'}).")
    lines.extend(_decision_source(d, members) for d in answer.decisions)
    return "\n".join(lines)


def _decision_source(d: Decision, members: dict[int, str]) -> str:
    when = datetime.fromtimestamp(d.decided_at).strftime("%d %b")
    who = members.get(d.decided_by, "a member")
    source = f", card {d.source.split(':', 1)[1]}" if d.source.startswith("proposal:") else ""
    return f"Source: “{d.question}” → {d.choice} (decided by {who} on {when}{source})."
