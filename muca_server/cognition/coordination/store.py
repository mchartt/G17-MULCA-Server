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

from .plan_writes import DependencyError, check_dependencies
from .models import (
    Charter,
    Decision,
    MemberProfile,
    Milestone,
    ProjectDocument,
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
    weekly_hours  INTEGER,
    background    TEXT NOT NULL DEFAULT ''
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
    task_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    owner_id     INTEGER,
    due_date     TEXT,
    status       TEXT NOT NULL DEFAULT 'todo',
    description  TEXT NOT NULL DEFAULT '',
    progress     INTEGER NOT NULL DEFAULT 0,  -- 0-100, set by a member; 100 when done
    created_at   REAL,                        -- unix time, set by TASK_TIMES
    done_at      REAL,                        -- unix time it became done, NULL otherwise
    touched_at   REAL                         -- unix time status or progress last changed (TASK_TIMES)
);
CREATE TABLE IF NOT EXISTS coord_task_dependency (
    task_id     TEXT NOT NULL,
    depends_on  TEXT NOT NULL,
    -- Where the link came from, for "why are these linked?": "card <id>: <the
    -- chat line behind it>", "by hand (<member>)"; '' = not known (older rows).
    source      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, depends_on)
);
CREATE TABLE IF NOT EXISTS coord_decision (
    decision_id  TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    choice       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    decided_by   INTEGER NOT NULL,
    decided_at   REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    reopened_at  REAL,
    reopened_by  INTEGER
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
-- When something last happened, kept so a server restart does not reset it:
-- "proactive" = message count and time of the last proactive card (R3, R4);
-- "checkin" = time of the last timed check-in (checkin_timer.py).
CREATE TABLE IF NOT EXISTS coord_mark (
    name           TEXT PRIMARY KEY,
    message_count  INTEGER NOT NULL DEFAULT 0,
    at             REAL NOT NULL
);
-- CS6 progress check-ins (progress.py): one row per task and deadline, so a
-- task is asked about at most once per due date (a new due date is a new key:
-- a check-in planned for the old one can never fire). Claimed with INSERT OR
-- IGNORE before the message is sent, so two timers, a restart or two workers
-- on the same file never send it twice. `answer` is what the member said
-- (on_track, behind, blocked, done, declined, skipped), read from their reply
-- by the ask path; coordinator memory only, never a change of the plan.
CREATE TABLE IF NOT EXISTS coord_progress_checkin (
    task_id      TEXT NOT NULL,
    due_date     TEXT NOT NULL,
    member_id    INTEGER,
    asked_at     REAL,
    answer       TEXT NOT NULL DEFAULT '',
    answered_at  REAL,
    PRIMARY KEY (task_id, due_date)
);
-- Choices of the group that are not project state: "language" = the language
-- of the coordinator's texts ("en" or "it"; no row = it follows the chat).
CREATE TABLE IF NOT EXISTS coord_setting (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
-- Documents the group gave Cadence as context (the assignment, notes): their
-- text only, never the file. Command "document"; kickoff adds them too.
CREATE TABLE IF NOT EXISTS coord_document (
    document_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    text         TEXT NOT NULL,
    added_by     INTEGER,
    added_at     REAL NOT NULL
);
-- "Helpful" on a proactive card: one row per member and card. Feedback
-- only: it never changes the project state or the card.
CREATE TABLE IF NOT EXISTS coord_feedback (
    proposal_id  TEXT NOT NULL,
    member_id    INTEGER NOT NULL,
    at           REAL NOT NULL,
    PRIMARY KEY (proposal_id, member_id)
);
"""


# When a task was made and when it was finished, kept by SQLite itself so every
# writer (by hand, cards, check-ins) is covered: the Reflection page counts
# "done this week" from done_at. Run after the columns exist (old files get
# them first, in _connect).
TASK_TIMES = """
CREATE TRIGGER IF NOT EXISTS coord_task_created AFTER INSERT ON coord_task
WHEN NEW.created_at IS NULL
BEGIN
    UPDATE coord_task SET created_at = (julianday('now') - 2440587.5) * 86400.0 WHERE task_id = NEW.task_id;
END;
CREATE TRIGGER IF NOT EXISTS coord_task_done AFTER UPDATE OF status ON coord_task
WHEN NEW.status = 'done' AND OLD.status <> 'done'
BEGIN
    UPDATE coord_task SET done_at = (julianday('now') - 2440587.5) * 86400.0, progress = 100 WHERE task_id = NEW.task_id;
END;
CREATE TRIGGER IF NOT EXISTS coord_task_touched AFTER UPDATE OF status, progress ON coord_task
WHEN NEW.status IS NOT OLD.status OR NEW.progress IS NOT OLD.progress
BEGIN
    UPDATE coord_task SET touched_at = (julianday('now') - 2440587.5) * 86400.0 WHERE task_id = NEW.task_id;
END;
CREATE TRIGGER IF NOT EXISTS coord_task_reopened AFTER UPDATE OF status ON coord_task
WHEN NEW.status <> 'done' AND OLD.status = 'done'
BEGIN
    UPDATE coord_task SET done_at = NULL WHERE task_id = NEW.task_id;
END;
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
                # Files made before "background" existed get the column.
                cursor = await db.execute("PRAGMA table_info(coord_member)")
                if "background" not in {row[1] for row in await cursor.fetchall()}:
                    await db.execute("ALTER TABLE coord_member ADD COLUMN background TEXT NOT NULL DEFAULT ''")
                # Files made before K3 get the "reopened" columns.
                cursor = await db.execute("PRAGMA table_info(coord_decision)")
                decision_columns = {row[1] for row in await cursor.fetchall()}
                if "reopened_at" not in decision_columns:
                    await db.execute("ALTER TABLE coord_decision ADD COLUMN reopened_at REAL")
                    await db.execute("ALTER TABLE coord_decision ADD COLUMN reopened_by INTEGER")
                # Files made before task descriptions get the column.
                cursor = await db.execute("PRAGMA table_info(coord_task)")
                task_columns = {row[1] for row in await cursor.fetchall()}
                if "description" not in task_columns:
                    await db.execute("ALTER TABLE coord_task ADD COLUMN description TEXT NOT NULL DEFAULT ''")
                # Files made before progress and times get them (old tasks: no times).
                if "progress" not in task_columns:
                    await db.execute("ALTER TABLE coord_task ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
                    await db.execute("ALTER TABLE coord_task ADD COLUMN created_at REAL")
                    await db.execute("ALTER TABLE coord_task ADD COLUMN done_at REAL")
                # Files made before CS6 progress check-ins get the column (old tasks: never touched).
                if "touched_at" not in task_columns:
                    await db.execute("ALTER TABLE coord_task ADD COLUMN touched_at REAL")
                # Files made before link provenance get the column (old links: unknown).
                cursor = await db.execute("PRAGMA table_info(coord_task_dependency)")
                if "source" not in {row[1] for row in await cursor.fetchall()}:
                    await db.execute("ALTER TABLE coord_task_dependency ADD COLUMN source TEXT NOT NULL DEFAULT ''")
                await db.executescript(TASK_TIMES)
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
                    (member_id, display_name, discipline, skills_json, weekly_hours, background)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(member_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    discipline   = excluded.discipline,
                    skills_json  = excluded.skills_json,
                    weekly_hours = excluded.weekly_hours,
                    background   = excluded.background
                """,
                (
                    profile.member_id,
                    profile.display_name,
                    profile.discipline,
                    json.dumps(profile.skills),
                    profile.weekly_hours,
                    profile.background,
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

    async def open_proposals(self, now: float) -> list[Proposal]:
        """Pending proposals not yet expired (R9: no second card on the same thing)."""
        db = await self._connect()
        db.row_factory = aiosqlite.Row
        try:
            rows = await _all_params(
                db, "SELECT * FROM coord_proposal WHERE status = 'pending' AND expires_at > ?", (now,)
            )
        finally:
            await db.close()
        return [_proposal_from_row(row) for row in rows]

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

    async def set_task_status(self, task_id: str, status: str) -> bool:
        """A member updates a task's status (their own action, M4). False if no such task."""
        db = await self._connect()
        try:
            cursor = await db.execute("UPDATE coord_task SET status = ? WHERE task_id = ?", (status, task_id))
            return cursor.rowcount > 0
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # The plan edited by hand (a member's own action: no card, no model)
    # ------------------------------------------------------------------

    async def add_task_by_hand(
        self,
        task_id: str,
        title: str,
        owner_id: int | None,
        due_date: str | None,
        description: str = "",
        depends_on: list[str] | None = None,
        source: str = "",
    ) -> None:
        """A new task; with `depends_on` the task and its dependencies are saved together or not at all."""
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO coord_task (task_id, title, owner_id, due_date, status, description) VALUES (?, ?, ?, ?, 'todo', ?)",
                    (task_id, title, owner_id, due_date, description),
                )
                if depends_on:
                    await _replace_dependencies(db, task_id, depends_on, source)
            except BaseException:
                await db.execute("ROLLBACK")
                raise
            await db.execute("COMMIT")
        finally:
            await db.close()

    async def edit_task(self, task_id: str, fields: dict, depends_on: list[str] | None = None, source: str = "") -> bool:
        """
        Set the given columns of a task (title, owner_id, due_date, status,
        description, progress; None clears owner or date) and, when `depends_on` is
        given, replace the tasks it waits on ([] = none). Raises
        DependencyError, saving nothing, if a dependency is not acceptable.
        """
        allowed = {k: v for k, v in fields.items() if k in ("title", "owner_id", "due_date", "status", "description", "progress")}
        if not allowed and depends_on is None:
            return False
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            try:
                found = True
                if allowed:
                    cursor = await db.execute(
                        f"UPDATE coord_task SET {', '.join(f'{column} = ?' for column in allowed)} WHERE task_id = ?",
                        (*allowed.values(), task_id),
                    )
                    found = cursor.rowcount > 0
                if found and depends_on is not None:
                    await _replace_dependencies(db, task_id, depends_on, source)
            except BaseException:
                await db.execute("ROLLBACK")
                raise
            await db.execute("COMMIT")
            return found
        finally:
            await db.close()

    async def remove_task(self, task_id: str) -> bool:
        db = await self._connect()
        try:
            await db.execute("DELETE FROM coord_task_dependency WHERE task_id = ? OR depends_on = ?", (task_id, task_id))
            cursor = await db.execute("DELETE FROM coord_task WHERE task_id = ?", (task_id,))
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def add_milestone(self, milestone_id: str, title: str, due_date: str | None) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO coord_milestone (milestone_id, title, due_date, position)
                VALUES (?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM coord_milestone))
                """,
                (milestone_id, title, due_date),
            )
        finally:
            await db.close()

    async def edit_milestone(self, milestone_id: str, fields: dict) -> bool:
        allowed = {k: v for k, v in fields.items() if k in ("title", "due_date")}
        if not allowed:
            return False
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"UPDATE coord_milestone SET {', '.join(f'{column} = ?' for column in allowed)} WHERE milestone_id = ?",
                (*allowed.values(), milestone_id),
            )
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def remove_milestone(self, milestone_id: str) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute("DELETE FROM coord_milestone WHERE milestone_id = ?", (milestone_id,))
            return cursor.rowcount > 0
        finally:
            await db.close()

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

    async def mark(self, name: str) -> tuple[int, float] | None:
        """(message count, time) saved under `name` ("proactive", "checkin"), or None."""
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT message_count, at FROM coord_mark WHERE name = ?", (name,))
            row = await cursor.fetchone()
        finally:
            await db.close()
        return (row[0], row[1]) if row else None

    async def save_mark(self, name: str, message_count: int, at: float) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO coord_mark (name, message_count, at) VALUES (?, ?, ?)",
                (name, message_count, at),
            )
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # CS6 progress check-ins (progress.py): coordinator memory, not the plan
    # ------------------------------------------------------------------

    async def claim_progress_checkin(self, task_id: str, due_date: str, member_id: int | None, at: float) -> bool:
        """Reserve the one check-in of this task for this due date. False: already asked (or answered)."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO coord_progress_checkin (task_id, due_date, member_id, asked_at) VALUES (?, ?, ?, ?)",
                (task_id, due_date, member_id, at),
            )
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def record_progress(self, task_id: str, due_date: str, answer: str, at: float) -> None:
        """What a member said about a task's progress for this due date (asked or not)."""
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO coord_progress_checkin (task_id, due_date, answer, answered_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (task_id, due_date) DO UPDATE SET answer = excluded.answer, answered_at = excluded.answered_at",
                (task_id, due_date, answer, at),
            )
        finally:
            await db.close()

    async def progress_checkins(self) -> list[dict]:
        """Every check-in row (task_id, due_date, member_id, asked_at, answer, answered_at)."""
        db = await self._connect()
        db.row_factory = aiosqlite.Row
        try:
            return [dict(row) for row in await _all(db, "SELECT * FROM coord_progress_checkin")]
        finally:
            await db.close()

    async def setting(self, name: str) -> str:
        """A choice of the group ("language"), or "" if it never made one."""
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT value FROM coord_setting WHERE name = ?", (name,))
            row = await cursor.fetchone()
        finally:
            await db.close()
        return row[0] if row else ""

    async def save_setting(self, name: str, value: str) -> None:
        """Save a choice of the group; "" removes it."""
        db = await self._connect()
        try:
            if value:
                await db.execute("INSERT OR REPLACE INTO coord_setting (name, value) VALUES (?, ?)", (name, value))
            else:
                await db.execute("DELETE FROM coord_setting WHERE name = ?", (name,))
        finally:
            await db.close()

    async def add_feedback(self, proposal_id: str, member_id: int, at: float) -> int:
        """A member found a card helpful (once per member). Returns how many did."""
        db = await self._connect()
        try:
            await db.execute(
                "INSERT OR IGNORE INTO coord_feedback (proposal_id, member_id, at) VALUES (?, ?, ?)",
                (proposal_id, member_id, at),
            )
            cursor = await db.execute("SELECT COUNT(*) FROM coord_feedback WHERE proposal_id = ?", (proposal_id,))
            row = await cursor.fetchone()
        finally:
            await db.close()
        return int(row[0])

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
                    background=row["background"],
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
            sources: dict[str, dict[str, str]] = {}
            for row in await _all(db, "SELECT * FROM coord_task_dependency"):
                dependencies.setdefault(row["task_id"], []).append(row["depends_on"])
                if row["source"]:
                    sources.setdefault(row["task_id"], {})[row["depends_on"]] = row["source"]
            tasks = [
                Task(
                    task_id=row["task_id"],
                    title=row["title"],
                    owner_id=row["owner_id"],
                    due_date=row["due_date"],
                    status=row["status"],
                    depends_on=sorted(dependencies.get(row["task_id"], [])),
                    dependency_sources=sources.get(row["task_id"], {}),
                    description=row["description"],
                    progress=row["progress"],
                    created_at=row["created_at"],
                    done_at=row["done_at"],
                    touched_at=row["touched_at"],
                )
                for row in await _all(db, "SELECT * FROM coord_task ORDER BY task_id")
            ]
            decisions = [
                Decision(**dict(row))
                for row in await _all(db, "SELECT * FROM coord_decision ORDER BY decided_at")
            ]
            documents = [
                ProjectDocument(
                    document_id=row["document_id"],
                    name=row["name"],
                    chars=row["chars"],
                    added_by=row["added_by"],
                    added_at=row["added_at"],
                )
                for row in await _all(
                    db, "SELECT document_id, name, length(text) AS chars, added_by, added_at FROM coord_document ORDER BY added_at"
                )
            ]
            language_rows = await _all(db, "SELECT value FROM coord_setting WHERE name = 'language'")
        finally:
            await db.close()
        return ProjectState(
            charter=charter,
            members=members,
            milestones=milestones,
            tasks=tasks,
            decisions=decisions,
            documents=documents,
            language=language_rows[0]["value"] if language_rows else "",
        )

    # ------------------------------------------------------------------
    # Project documents (a member's own action: kept as text, never as a file)
    # ------------------------------------------------------------------

    async def add_document(self, document_id: str, name: str, text: str, added_by: int | None, added_at: float) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO coord_document (document_id, name, text, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                (document_id, name, text, added_by, added_at),
            )
        finally:
            await db.close()

    async def remove_document(self, document_id: str) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute("DELETE FROM coord_document WHERE document_id = ?", (document_id,))
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def document_texts(self) -> list[tuple[str, str]]:
        """(name, text) of every document, oldest first: what Cadence reads when it answers."""
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT name, text FROM coord_document ORDER BY added_at")
            return [(row[0], row[1]) for row in await cursor.fetchall()]
        finally:
            await db.close()


async def _all_params(db: aiosqlite.Connection, sql: str, params: tuple) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql, params)
    return list(await cursor.fetchall())


async def _all(db: aiosqlite.Connection, sql: str) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql)
    return list(await cursor.fetchall())


async def _replace_dependencies(db: aiosqlite.Connection, task_id: str, depends_on: list[str], source: str = "") -> None:
    """
    `task_id` now waits on exactly `depends_on`. Checked inside the caller's
    transaction, so two members changing the plan at once cannot sneak a loop in.
    Links kept from before keep their source; new ones get `source`.
    """
    wanted = list(dict.fromkeys(depends_on))
    await check_dependencies(db, task_id, wanted, replacing=True)
    kept = {row[0]: row[1] for row in await _all_params(
        db, "SELECT depends_on, source FROM coord_task_dependency WHERE task_id = ?", (task_id,))}
    await db.execute("DELETE FROM coord_task_dependency WHERE task_id = ?", (task_id,))
    for other in wanted:
        await db.execute(
            "INSERT INTO coord_task_dependency (task_id, depends_on, source) VALUES (?, ?, ?)",
            (task_id, other, kept.get(other, source)),
        )


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
