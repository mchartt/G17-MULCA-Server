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


async def add_dependency(db: aiosqlite.Connection, *, task_id: str, depends_on: str) -> None:
    """`task_id` waits on `depends_on`."""
    await db.execute(
        "INSERT OR IGNORE INTO coord_task_dependency (task_id, depends_on) VALUES (?, ?)",
        (task_id, depends_on),
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
