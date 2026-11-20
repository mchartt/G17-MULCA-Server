"""
Writes of the plan (tasks, decisions) and of "Not now", used by the appliers
(appliers.py) **inside** CoordinationStore.resolve_proposal's transaction.

Every function receives the open connection and never commits: the caller's
transaction commits the proposal's new status and these rows together, or
neither. Kept apart from store.py so that file stays about reading and the
proposal life cycle (and under the project's file-size budget).
"""

from __future__ import annotations

import re
from datetime import date

import aiosqlite

from .models import Decision, TaskStatus


def topic_key(topic: str) -> str:
    """
    The key "Not now" silences: the topic in lower case, letters and digits
    only, single spaces, at most 60 characters. "Fonts & margins!" and
    "fonts and margins" are not the same key: matching stays deterministic
    and explainable, the model is told the silenced topics too (proactive.py).
    """
    words = re.findall(r"\w+", topic.lower())
    return " ".join(words)[:60]


async def task_owner(db: aiosqlite.Connection, task_id: str) -> tuple[bool, int | None]:
    """(exists, owner_id) of a task, read inside the transaction."""
    cursor = await db.execute("SELECT owner_id FROM coord_task WHERE task_id = ?", (task_id,))
    row = await cursor.fetchone()
    return (row is not None, row[0] if row else None)


async def add_task(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    title: str,
    owner_id: int | None,
    due_date: date | None,
) -> None:
    """A new task in the plan (status todo)."""
    await db.execute(
        "INSERT INTO coord_task (task_id, title, owner_id, due_date, status) VALUES (?, ?, ?, ?, ?)",
        (task_id, title, owner_id, _iso(due_date), TaskStatus.TODO.value),
    )


async def assign_task(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    owner_id: int,
    due_date: date | None,
) -> None:
    """Give an existing task its owner, and its date when the card has one."""
    if due_date is None:
        await db.execute("UPDATE coord_task SET owner_id = ? WHERE task_id = ?", (owner_id, task_id))
    else:
        await db.execute(
            "UPDATE coord_task SET owner_id = ?, due_date = ? WHERE task_id = ?",
            (owner_id, due_date.isoformat(), task_id),
        )


async def update_task(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    due_date: date | None = None,
    owner_id: int | None = None,
    status: TaskStatus | None = None,
) -> None:
    """Change only the given fields of a task (check-in options, blockers, status)."""
    if due_date is not None:
        await db.execute("UPDATE coord_task SET due_date = ? WHERE task_id = ?", (due_date.isoformat(), task_id))
    if owner_id is not None:
        await db.execute("UPDATE coord_task SET owner_id = ? WHERE task_id = ?", (owner_id, task_id))
    if status is not None:
        await db.execute("UPDATE coord_task SET status = ? WHERE task_id = ?", (status.value, task_id))


async def delete_task(db: aiosqlite.Connection, task_id: str) -> None:
    """Drop a task and its dependencies (a check-in option the group chose)."""
    await db.execute("DELETE FROM coord_task_dependency WHERE task_id = ? OR depends_on = ?", (task_id, task_id))
    await db.execute("DELETE FROM coord_task WHERE task_id = ?", (task_id,))


class DependencyError(ValueError):
    """
    A dependency the plan cannot hold. `kind` is "unknown" (no such task),
    "itself" (a task cannot wait on itself) or "loop" (A waits on B and B,
    through any tasks, waits on A: neither could ever start).
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


async def check_dependencies(db: aiosqlite.Connection, task_id: str, wanted: list[str], *, replacing: bool = False) -> None:
    """
    Raise DependencyError if `task_id` cannot wait on `wanted`: itself, a task
    not in the plan, or a loop. The one check for every way a link is made
    (by hand, or a confirmed card), inside the caller's transaction.
    `replacing`: the task's own current links are about to be replaced, so
    they are left out of the loop search.
    """
    if task_id in wanted:
        raise DependencyError("itself")
    for other in wanted:
        cursor = await db.execute("SELECT 1 FROM coord_task WHERE task_id = ?", (other,))
        if await cursor.fetchone() is None:
            raise DependencyError("unknown")
    waits_on: dict[str, list[str]] = {}
    cursor = await db.execute("SELECT task_id, depends_on FROM coord_task_dependency")
    for row in await cursor.fetchall():
        if not (replacing and row[0] == task_id):
            waits_on.setdefault(row[0], []).append(row[1])
    # A loop closes if, following "waits on" from any wanted task, we come back to this one.
    to_visit, seen = list(wanted), set()
    while to_visit:
        current = to_visit.pop()
        if current == task_id:
            raise DependencyError("loop")
        if current not in seen:
            seen.add(current)
            to_visit.extend(waits_on.get(current, []))


def known_prerequisite(waiting, depends_on_title: str, depends_on_owner_id, tasks) -> str | None:
    """
    The id of a task `waiting` already waits on that is the same work as a new
    prerequisite (same title, or same member delivering it), or None. Saying
    "Mathias must send me the data" again after the link was confirmed must
    not add a second "data" task.
    """
    if waiting is None:
        return None
    by_id = {t.task_id: t for t in tasks}
    key = topic_key(depends_on_title)
    for other_id in waiting.depends_on:
        other = by_id.get(other_id)
        if other is None:
            continue
        if (key and topic_key(other.title) == key) or (
            depends_on_owner_id is not None and other.owner_id == depends_on_owner_id
        ):
            return other_id
    return None


def same_title_task(title: str, tasks, *, besides: str | None = None) -> str | None:
    """The id of a plan task with exactly this title (ignoring case and punctuation), or None."""
    key = topic_key(title)
    return next((t.task_id for t in tasks if key and topic_key(t.title) == key and t.task_id != besides), None)


async def add_dependency(db: aiosqlite.Connection, *, task_id: str, depends_on: str, source: str = "") -> None:
    """`task_id` waits on `depends_on` (checked: no self-link, no loop; a repeat is a no-op)."""
    await check_dependencies(db, task_id, [depends_on])
    await db.execute(
        "INSERT OR IGNORE INTO coord_task_dependency (task_id, depends_on, source) VALUES (?, ?, ?)",
        (task_id, depends_on, source),
    )


async def add_decision(db: aiosqlite.Connection, decision: Decision) -> None:
    """One closed decision in the decision log (FR10)."""
    await db.execute(
        """
        INSERT INTO coord_decision
            (decision_id, question, choice, reason, decided_by, decided_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision.decision_id,
            decision.question,
            decision.choice,
            decision.reason,
            decision.decided_by,
            decision.decided_at,
            decision.source,
        ),
    )


async def reopen_decision(db: aiosqlite.Connection, decision_id: str, *, member_id: int, now: float) -> bool:
    """K3: mark a decision reopened (kept in the log). False if it is gone or already reopened."""
    cursor = await db.execute(
        "UPDATE coord_decision SET reopened_at = ?, reopened_by = ? WHERE decision_id = ? AND reopened_at IS NULL",
        (now, member_id, decision_id),
    )
    return cursor.rowcount > 0


async def add_snooze(
    db: aiosqlite.Connection,
    *,
    strategy_id: str,
    topic: str,
    until: float,
) -> None:
    """"Not now": silence this strategy on this topic until `until`."""
    await db.execute(
        """
        INSERT INTO coord_snooze (strategy_id, topic_key, until) VALUES (?, ?, ?)
        ON CONFLICT(strategy_id, topic_key) DO UPDATE SET until = excluded.until
        """,
        (strategy_id, topic_key(topic), until),
    )


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
