"""
Project memory: what the intervention agent knows about the plan (K1).

Before this module the agent that decides *whether* a coordinator strategy
fires saw only the chat. It could not notice that Sara's test on Friday needs
Marco's API, which is not done, or that a member is proposing again an option
the group already turned down: both facts live in the plan and in the
decision log, not in the last messages.

`project_memory(...)` turns the saved state into a short, plain-text block
that the policy appends to the observer's turn update (policy.py,
`_project_note`), only when the coordinator is on. It is built in code from
the database, never by a model, so it can only say what the group confirmed.

Kept small on purpose (the observer reads it on every judged turn):
at most MAX_TASKS open tasks, MAX_DECISIONS recent decisions and the
milestones of the next MILESTONE_DAYS days. The bridge sends it again only
when it changes, or every RESEND_EVERY messages so it is not lost when the
observer's context is trimmed.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import ProjectState, Task, TaskStatus

MAX_TASKS = 15
MAX_DECISIONS = 8
MILESTONE_DAYS = 14
RESEND_EVERY = 25  # messages
MAX_REASON = 160

HEADER = (
    "Project memory (saved plan and decision log of this group; it is "
    "context for judging the coordinator strategies, not a message to reply to)"
)


def project_memory(state: ProjectState, members: dict[int, str], today: date) -> str:
    """The block, or "" when there is nothing saved yet (no tasks, no decisions, no milestones)."""
    open_tasks = [t for t in state.tasks if t.status is not TaskStatus.DONE]
    by_id = {t.task_id: t for t in state.tasks}
    horizon = today + timedelta(days=MILESTONE_DAYS)
    milestones = [m for m in state.milestones if m.due_date is not None and today <= m.due_date <= horizon]
    recent = sorted(state.decisions, key=lambda d: d.decided_at, reverse=True)[:MAX_DECISIONS]
    decisions = [d for d in recent if d.reopened_at is None]
    reopened = [d for d in recent if d.reopened_at is not None]
    if not (open_tasks or milestones or recent):
        return ""

    lines = [f"{HEADER}. Today is {_day(today)} ({today.isoformat()})."]
    if state.charter is not None:
        lines.append(f"Goal: {_one_line(state.charter.goal, 200)}")
    if open_tasks:
        lines.append("Open tasks:")
        for task in _by_urgency(open_tasks)[:MAX_TASKS]:
            lines.append(f"- {_task_line(task, by_id, members)}")
        if len(open_tasks) > MAX_TASKS:
            lines.append(f"- … and {len(open_tasks) - MAX_TASKS} more")
    if milestones:
        lines.append("Next milestones:")
        for milestone in sorted(milestones, key=lambda m: m.due_date):
            lines.append(f"- “{milestone.title}” on {_day(milestone.due_date)}")
    if decisions:
        lines.append("Decisions already taken (most recent first):")
        for decision in decisions:
            reason = f" because {_one_line(decision.reason, MAX_REASON)}" if decision.reason.strip() else ""
            lines.append(f"- {_one_line(decision.question, 120)} → chose “{decision.choice}”{reason}")
    if reopened:
        lines.append("Reopened by the group (open again, being discussed; do not treat as decided):")
        for decision in reopened:
            lines.append(f"- {_one_line(decision.question, 120)} (was “{decision.choice}”)")
    return "\n".join(lines)


def _task_line(task: Task, by_id: dict[str, Task], members: dict[int, str]) -> str:
    owner = members.get(task.owner_id, "unknown member") if task.owner_id is not None else "no owner"
    due = f"due {_day(task.due_date)}" if task.due_date else "no date"
    parts = [f"“{task.title}”", owner, due, task.status.value]
    waits = [by_id[d] for d in task.depends_on if d in by_id]
    if waits:
        parts.append(
            "waits on " + ", ".join(f"“{w.title}” ({w.status.value}{', due ' + _day(w.due_date) if w.due_date else ''})" for w in waits)
        )
    return " | ".join(parts)


def _by_urgency(tasks: list[Task]) -> list[Task]:
    """Blocked first, then by date (no date last), so the cut keeps what matters."""
    far = date.max
    return sorted(tasks, key=lambda t: (t.status is not TaskStatus.BLOCKED, t.due_date or far, t.title))


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _day(value: date) -> str:
    return f"{value:%a} {value.day} {value:%b}"
