"""
CoordinationStore: the project state on disk, and the only code that writes it.

The tables live in the conversation's own SQLite file (the same file
cognition/memory/sqlite.py uses, see db_path_for), so a group's memory and
its project state are deleted, exported and backed up together. Table names
start with `coord_` so they never collide with the lab's tables.

Like cognition/memory/sqlite.py, every call opens its own short connection
(`aiosqlite.connect` per call): there is no shared connection to manage.

The one subtle method is `resolve_proposal`: it closes a proposal and writes
its change in a single transaction, and only if the proposal is still
pending. That is what makes "the first valid click wins" true even when two
members click at the same instant on two different channels.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

from .models import (
    Charter,
    Decision,
    MemberProfile,
    Milestone,
    ProjectState,
    Proposal,
    ProposalStatus,
    Task,
)

# A function that writes a proposal's change, inside resolve_proposal's
# transaction. It receives the open connection and must not commit.
Applier = Callable[[aiosqlite.Connection], Awaitable[None]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_member (
    member_id     INTEGER PRIMARY KEY,
    display_name  TEXT NOT NULL,
    discipline    TEXT NOT NULL DEFAULT '',
    skills_json   TEXT NOT NULL DEFAULT '[]',
    weekly_hours  INTEGER
);
CREATE TABLE IF NOT EXISTS coord_charter (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),  -- one charter per group
    goal               TEXT NOT NULL,
    deliverables_json  TEXT NOT NULL DEFAULT '[]',
    confirmed_by       INTEGER NOT NULL,
    confirmed_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS coord_milestone (
    milestone_id  TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    due_date      TEXT,
    position      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coord_task (
    task_id   TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    owner_id  INTEGER,
    due_date  TEXT,
    status    TEXT NOT NULL DEFAULT 'todo'
);
CREATE TABLE IF NOT EXISTS coord_task_dependency (
    task_id     TEXT NOT NULL,
    depends_on  TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on)
);
CREATE TABLE IF NOT EXISTS coord_decision (
    decision_id  TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    choice       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    decided_by   INTEGER NOT NULL,
    decided_at   REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coord_proposal (
    proposal_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    rationale       TEXT NOT NULL DEFAULT '',
    origin_channel  TEXT NOT NULL DEFAULT 'discord',
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    resolved_by     INTEGER,
    resolved_at     REAL
);
-- "Not now" on a proactive card: that strategy stays silent on that topic
-- until `until` (unix time). Written by plan_writes.add_snooze.
CREATE TABLE IF NOT EXISTS coord_snooze (
    strategy_id  TEXT NOT NULL,
    topic_key    TEXT NOT NULL,
    until        REAL NOT NULL,
    PRIMARY KEY (strategy_id, topic_key)
);
"""


class CoordinationStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._ready = False

    async def _connect(self) -> aiosqlite.Connection:
        """
        A new connection in autocommit mode (isolation_level=None): plain
        statements commit at once, and resolve_proposal opens its own
        explicit transaction.
        """
        db = await aiosqlite.connect(self._db_path, isolation_level=None)
        if not self._ready:
            try:
                await db.executescript(SCHEMA)
            except Exception:
                await db.close()
                raise
            self._ready = True
        return db

    # ------------------------------------------------------------------
    # Members (written by the member's own command, never by the model)
    # ------------------------------------------------------------------

    async def upsert_member(self, profile: MemberProfile) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO coord_member
                    (member_id, display_name, discipline, skills_json, weekly_hours)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(member_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    discipline   = excluded.discipline,
                    skills_json  = excluded.skills_json,
                    weekly_hours = excluded.weekly_hours
                """,
                (
                    profile.member_id,
                    profile.display_name,
                    profile.discipline,
                    json.dumps(profile.skills),
                    profile.weekly_hours,
                ),
            )
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------

    async def add_proposal(self, proposal: Proposal) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO coord_proposal
                    (proposal_id, kind, status, payload_json, rationale,
                     origin_channel, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.kind.value,
                    proposal.status.value,
                    json.dumps(proposal.payload),
                    proposal.rationale,
                    proposal.origin_channel,
                    proposal.created_at,
                    proposal.expires_at,
                ),
            )
        finally:
            await db.close()

    async def get_proposal(self, proposal_id: str) -> Proposal | None:
        db = await self._connect()
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute(
                "SELECT * FROM coord_proposal WHERE proposal_id = ?",
                (proposal_id,),
            )
            row = await cursor.fetchone()
        finally:
            await db.close()
        return _proposal_from_row(row) if row is not None else None

    async def resolve_proposal(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        member_id: int | None,
        now: float,
        apply: Applier | None = None,
    ) -> bool:
        """
        Close a pending proposal with `status`, and run `apply` in the same
        transaction. Returns False, changing nothing, if the proposal was not
        pending any more (someone else clicked first, or it expired).
        """
        db = await self._connect()
        try:
            # IMMEDIATE takes the write lock now, so two clicks cannot both
            # see "pending" and both apply.
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE coord_proposal
                SET status = ?, resolved_by = ?, resolved_at = ?
                WHERE proposal_id = ? AND status = ?
                """,
                (
                    status.value,
                    member_id,
                    now,
                    proposal_id,
                    ProposalStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                await db.execute("ROLLBACK")
                return False
            try:
                if apply is not None:
                    await apply(db)
            except Exception:
                # The change failed: the proposal stays pending, nothing is half-written.
                await db.execute("ROLLBACK")
                raise
            await db.execute("COMMIT")
            return True
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Writers used by appliers (inside resolve_proposal's transaction)
    # ------------------------------------------------------------------

    @staticmethod
    async def charter_confirmed_at(db: aiosqlite.Connection) -> float | None:
        """When the current charter was confirmed, or None (inside a transaction)."""
        cursor = await db.execute("SELECT confirmed_at FROM coord_charter WHERE id = 1")
        row = await cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    async def write_charter(
        db: aiosqlite.Connection,
        charter: Charter,
        milestones: list[Milestone],
    ) -> None:
        """Replace the charter and its milestones (a new kickoff replaces the old one)."""
        await db.execute(
            """
            INSERT OR REPLACE INTO coord_charter
                (id, goal, deliverables_json, confirmed_by, confirmed_at)
            VALUES (1, ?, ?, ?, ?)
            """,
            (
                charter.goal,
                json.dumps(charter.deliverables),
                charter.confirmed_by,
                charter.confirmed_at,
            ),
        )
        await db.execute("DELETE FROM coord_milestone")
        await db.executemany(
            """
            INSERT INTO coord_milestone (milestone_id, title, due_date, position)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    m.milestone_id,
                    m.title,
                    m.due_date.isoformat() if m.due_date else None,
                    position,
                )
                for position, m in enumerate(milestones)
            ],
        )

    async def active_snoozes(self, now: float) -> set[tuple[str, str]]:
        """(strategy_id, topic_key) pairs silenced by "Not now" and not yet over."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT strategy_id, topic_key FROM coord_snooze WHERE until > ?",
                (now,),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()
        return {(row[0], row[1]) for row in rows}

    # ------------------------------------------------------------------
    # Reading the whole state
    # ------------------------------------------------------------------

    async def load_state(self) -> ProjectState:
        db = await self._connect()
        db.row_factory = aiosqlite.Row
        try:
            members = [
                MemberProfile(
                    member_id=row["member_id"],
                    display_name=row["display_name"],
                    discipline=row["discipline"],
                    skills=json.loads(row["skills_json"]),
                    weekly_hours=row["weekly_hours"],
                )
                for row in await _all(db, "SELECT * FROM coord_member ORDER BY display_name")
            ]
            charter_rows = await _all(db, "SELECT * FROM coord_charter")
            charter = (
                Charter(
                    goal=charter_rows[0]["goal"],
                    deliverables=json.loads(charter_rows[0]["deliverables_json"]),
                    confirmed_by=charter_rows[0]["confirmed_by"],
                    confirmed_at=charter_rows[0]["confirmed_at"],
                )
                if charter_rows
                else None
            )
            milestones = [
                Milestone(
                    milestone_id=row["milestone_id"],
                    title=row["title"],
                    due_date=row["due_date"],
                )
                for row in await _all(db, "SELECT * FROM coord_milestone ORDER BY position")
            ]
            dependencies: dict[str, list[str]] = {}
            for row in await _all(db, "SELECT * FROM coord_task_dependency"):
                dependencies.setdefault(row["task_id"], []).append(row["depends_on"])
            tasks = [
                Task(
                    task_id=row["task_id"],
                    title=row["title"],
                    owner_id=row["owner_id"],
                    due_date=row["due_date"],
                    status=row["status"],
                    depends_on=sorted(dependencies.get(row["task_id"], [])),
                )
                for row in await _all(db, "SELECT * FROM coord_task ORDER BY task_id")
            ]
            decisions = [
                Decision(**dict(row))
                for row in await _all(db, "SELECT * FROM coord_decision ORDER BY decided_at")
            ]
        finally:
            await db.close()
        return ProjectState(
            charter=charter,
            members=members,
            milestones=milestones,
            tasks=tasks,
            decisions=decisions,
        )


async def _all(db: aiosqlite.Connection, sql: str) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql)
    return list(await cursor.fetchall())


def _proposal_from_row(row: aiosqlite.Row) -> Proposal:
    return Proposal(
        proposal_id=row["proposal_id"],
        kind=row["kind"],
        status=row["status"],
        payload=json.loads(row["payload_json"]),
        rationale=row["rationale"],
        origin_channel=row["origin_channel"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        resolved_by=row["resolved_by"],
        resolved_at=row["resolved_at"],
    )
