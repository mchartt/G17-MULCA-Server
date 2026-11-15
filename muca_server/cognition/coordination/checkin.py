"""
Check-in (M4, spec S6 / UC6): plan against reality, computed in code.

`findings(state, members, today)` lists what is late or at risk. It is pure
code, no model: the model only proposes up to three ways to fix these
findings (plan_writer.py), and the card shows the findings as the server
computed them, so "what is wrong" can never be invented.

What counts as a finding (one line each, in the members' terms):
    late          a task not done, due before today
    due soon      a task not done, due within SOON_DAYS
    blocked       a task with status blocked
    no owner      a task nobody owns
    milestone     a milestone within SOON_DAYS with tasks not done due on or before it
    dependency    a task waits on one that is due later than itself, or not done and late
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import ProjectState, Task, TaskStatus

SOON_DAYS = 2  # spec FR15: "milestone within 48 hours with open tasks"


@dataclass(frozen=True, slots=True)
class Finding:
    text: str
    task_id: str | None = None


def findings(state: ProjectState, members: dict[int, str], today: date) -> list[Finding]:
    """Everything late or at risk, most urgent first (late, blocked, soon, the rest)."""
    open_tasks = [t for t in state.tasks if t.status is not TaskStatus.DONE]
    by_id = {t.task_id: t for t in state.tasks}
    soon = today + timedelta(days=SOON_DAYS)
    late, blocked, due_soon, rest = [], [], [], []

    for task in open_tasks:
        who = _owner(task, members)
        if task.due_date is not None and task.due_date < today:
            late.append(Finding(f"Late: “{task.title}” ({who}) was due {_day(task.due_date)}.", task.task_id))
        elif task.due_date is not None and task.due_date <= soon:
            due_soon.append(Finding(f"Due soon: “{task.title}” ({who}) is due {_day(task.due_date)}.", task.task_id))
        if task.status is TaskStatus.BLOCKED:
            blocked.append(Finding(f"Blocked: “{task.title}” ({who}).", task.task_id))
        if task.owner_id is None:
            rest.append(Finding(f"No owner: “{task.title}”.", task.task_id))
        for other_id in task.depends_on:
            other = by_id.get(other_id)
            if other is None or other.status is TaskStatus.DONE:
                continue
            if task.due_date and other.due_date and other.due_date > task.due_date:
                rest.append(
                    Finding(
                        f"Dependency: “{task.title}” waits on “{other.title}”, due after it ({_day(other.due_date)}).",
                        other.task_id,
                    )
                )

    for milestone in state.milestones:
        if milestone.due_date is None or not today <= milestone.due_date <= soon:
            continue
        pending = [t for t in open_tasks if t.due_date is None or t.due_date <= milestone.due_date]
        if pending:
            rest.append(
                Finding(f"Milestone “{milestone.title}” on {_day(milestone.due_date)} with {len(pending)} open task(s).")
            )
    return late + blocked + due_soon + rest


def owners_report(state: ProjectState, members: dict[int, str]) -> list[str]:
    """One line per member: their open tasks with status and date (the "ask the owners" part)."""
    lines = []
    for member_id, name in sorted(members.items(), key=lambda kv: kv[1]):
        mine = [t for t in state.tasks if t.owner_id == member_id and t.status is not TaskStatus.DONE]
        if mine:
            parts = ", ".join(f"{t.title} ({t.status.value}{', ' + _day(t.due_date) if t.due_date else ''})" for t in mine)
            lines.append(f"{name}: {parts}")
    return lines


def _owner(task: Task, members: dict[int, str]) -> str:
    return members.get(task.owner_id, "no owner") if task.owner_id else "no owner"


def _day(value: date) -> str:
    return f"{value.day} {value:%b}"
