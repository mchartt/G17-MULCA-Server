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

import re
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


# The findings are written in English (the model and the tests read them);
# an Italian group reads them in Italian on the card (22/11 live: "Due soon:
# … is due 24 Nov." inside an Italian card).
_MONTHS_IT = dict(zip(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
    "gen feb mar apr mag giu lug ago set ott nov dic".split(),
))
_DAY = r"(\d{1,2}) ([A-Z][a-z]{2})"


def _day_it(day: str, month: str) -> str:
    return f"{day} {_MONTHS_IT.get(month, month)}"


def _who_it(who: str) -> str:
    return "senza responsabile" if who == "no owner" else who


_ITALIAN = [
    (re.compile(rf"^Late: “(.*)” \((.*)\) was due {_DAY}\.$"),
     lambda m: f"In ritardo: «{m[1]}» ({_who_it(m[2])}) era per il {_day_it(m[3], m[4])}."),
    (re.compile(rf"^Due soon: “(.*)” \((.*)\) is due {_DAY}\.$"),
     lambda m: f"In scadenza: «{m[1]}» ({_who_it(m[2])}) è per il {_day_it(m[3], m[4])}."),
    (re.compile(r"^Blocked: “(.*)” \((.*)\)\.$"), lambda m: f"Bloccata: «{m[1]}» ({_who_it(m[2])})."),
    (re.compile(r"^No owner: “(.*)”\.$"), lambda m: f"Senza responsabile: «{m[1]}»."),
    (re.compile(rf"^Dependency: “(.*)” waits on “(.*)”, due after it \({_DAY}\)\.$"),
     lambda m: f"Dipendenza: «{m[1]}» aspetta «{m[2]}», che scade dopo ({_day_it(m[3], m[4])})."),
    (re.compile(rf"^Milestone “(.*)” on {_DAY} with (\d+) open task\(s\)\.$"),
     lambda m: f"Milestone «{m[1]}» il {_day_it(m[2], m[3])} con {m[4]} attività aperte."),
]


def in_italian(text: str) -> str:
    """One finding in Italian; a sentence this does not know stays as it is."""
    for pattern, write in _ITALIAN:
        match = pattern.match(text)
        if match:
            return write(match)
    return text
