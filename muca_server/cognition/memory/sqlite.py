"""
DB-backed EpisodicStore/SnapshotStore, on SQLite via aiosqlite.

Schema mirrors the old MULCA prototype's Django models (EpisodicLog,
WorkingMemorySnapshot) -- same fields, same semantics -- ported to plain SQL
since this project has no ORM. One connection per call rather than one
held open for the process lifetime: simpler, and safe under SQLite's own
file-level locking at the write volume this project has (one row per turn,
not a high-throughput log).

RAG extension point, not built here: retrieval today is purely
recency-window (recall_recent), the same limitation the in-memory store
has. Closing that gap needs no schema change to this table -- a future
embeddings/FTS side-table can key off episodic_log's autoincrement `id`,
and EpisodicStore.search() (or similar) can be added to the port without
touching recall_recent() or any of its callers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import aiosqlite

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def db_path_for(conversation_id: str, *, base_dir: str | Path) -> Path:
    """
    Turn a conversation_id (e.g. "instance:team-alpha", "dm:123456789") into
    a stable, filesystem-safe SQLite file path -- one file per conversation,
    deterministically re-derived from the id every time, so nothing about
    "which file does this conversation use" needs to be separately recorded
    or looked up anywhere.
    """

    safe_name = _UNSAFE_FILENAME_CHARS.sub("_", conversation_id)
    return Path(base_dir) / f"{safe_name}.db"

from .records import EpisodicRecord, MemorySnapshot, StoredUtterance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    conversation_id TEXT NOT NULL,
    intervention_path TEXT NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    trigger_variables TEXT NOT NULL DEFAULT '{}',
    strategy_id TEXT,
    prompt_fragment TEXT,
    assistant_output TEXT,
    history_summary TEXT,
    participant_roles_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodic_log_conversation
    ON episodic_log (conversation_id, timestamp);

CREATE TABLE IF NOT EXISTS memory_snapshot (
    conversation_id TEXT PRIMARY KEY,
    history_summary TEXT NOT NULL DEFAULT '',
    participant_roles_summary TEXT NOT NULL DEFAULT '',
    history_tail TEXT NOT NULL DEFAULT '[]',
    total_message_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(_SCHEMA)


class SqliteEpisodicStore:
    """Satisfies memory.ports.EpisodicStore against a SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def log(self, record: EpisodicRecord) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute(
                """
                INSERT INTO episodic_log (
                    timestamp, conversation_id, intervention_path, speaker,
                    content, trigger_variables, strategy_id, prompt_fragment,
                    assistant_output, history_summary, participant_roles_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp,
                    record.conversation_id,
                    record.intervention_path,
                    record.speaker,
                    record.content,
                    json.dumps(record.trigger_variables),
                    record.strategy_id,
                    record.prompt_fragment,
                    record.assistant_output,
                    record.history_summary,
                    record.participant_roles_summary,
                ),
            )
            await db.commit()

    async def recall_recent(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[EpisodicRecord]:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            db.row_factory = aiosqlite.Row

            # Recency window, not relevance search -- newest first here,
            # then reversed below so callers get them chronological,
            # matching the in-memory store's contract.
            cursor = await db.execute(
                """
                SELECT * FROM episodic_log
                WHERE conversation_id = ? AND intervention_path != 'management_cycle'
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            )
            rows = await cursor.fetchall()

        return [
            EpisodicRecord(
                timestamp=row["timestamp"],
                conversation_id=row["conversation_id"],
                intervention_path=row["intervention_path"],
                speaker=row["speaker"],
                content=row["content"],
                trigger_variables=json.loads(row["trigger_variables"]),
                strategy_id=row["strategy_id"],
                prompt_fragment=row["prompt_fragment"],
                assistant_output=row["assistant_output"],
                history_summary=row["history_summary"],
                participant_roles_summary=row["participant_roles_summary"],
            )
            for row in reversed(rows)
        ]


class SqliteSnapshotStore:
    """Satisfies memory.ports.SnapshotStore against a SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def load(
        self,
        conversation_id: str,
    ) -> MemorySnapshot | None:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM memory_snapshot WHERE conversation_id = ?",
                (conversation_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return MemorySnapshot(
            history_summary=row["history_summary"],
            participant_roles_summary=row["participant_roles_summary"],
            history_tail=[
                StoredUtterance(**item)
                for item in json.loads(row["history_tail"])
            ],
            total_message_count=row["total_message_count"],
            updated_at=row["updated_at"],
        )

    async def save(
        self,
        conversation_id: str,
        snapshot: MemorySnapshot,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute(
                """
                INSERT INTO memory_snapshot (
                    conversation_id, history_summary, participant_roles_summary,
                    history_tail, total_message_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    history_summary = excluded.history_summary,
                    participant_roles_summary = excluded.participant_roles_summary,
                    history_tail = excluded.history_tail,
                    total_message_count = excluded.total_message_count,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    snapshot.history_summary,
                    snapshot.participant_roles_summary,
                    json.dumps(
                        [utterance.model_dump() for utterance in snapshot.history_tail]
                    ),
                    snapshot.total_message_count,
                    snapshot.updated_at,
                ),
            )
            await db.commit()
