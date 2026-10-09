"""
DB-backed EpisodicStore/SnapshotStore, on SQLite via aiosqlite.

Schema mirrors the old MULCA prototype's Django models (EpisodicLog,
WorkingMemorySnapshot) -- same fields, same semantics -- ported to plain SQL
since this project has no ORM. One connection per call rather than one
held open for the process lifetime: simpler, and safe under SQLite's own
file-level locking at the write volume this project has (one row per turn,
not a high-throughput log).

search_similar() is the RAG extension point this module's docstring used
to flag as "not built here": a side table (episodic_embedding) keyed off
episodic_log's autoincrement id, exactly as anticipated -- recall_recent()
and its callers are untouched.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiosqlite
import numpy as np

from .ports import Embedder
from .similarity import rank_by_similarity

logger = logging.getLogger(__name__)

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
    modality TEXT NOT NULL DEFAULT 'text',
    trigger_variables TEXT NOT NULL DEFAULT '{}',
    strategy_id TEXT,
    prompt_fragment TEXT,
    assistant_output TEXT,
    history_summary TEXT,
    participant_roles_summary TEXT,
    action_points TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodic_log_conversation
    ON episodic_log (conversation_id, timestamp);

CREATE TABLE IF NOT EXISTS episodic_embedding (
    episodic_log_id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_embedding_conversation
    ON episodic_embedding (conversation_id);

CREATE TABLE IF NOT EXISTS memory_snapshot (
    conversation_id TEXT PRIMARY KEY,
    history_summary TEXT NOT NULL DEFAULT '',
    participant_roles_summary TEXT NOT NULL DEFAULT '',
    action_points TEXT NOT NULL DEFAULT '',
    history_tail TEXT NOT NULL DEFAULT '[]',
    total_message_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    recent_actions TEXT NOT NULL DEFAULT '{}',
    recent_action_at TEXT NOT NULL DEFAULT '{}',
    full_context_sent_at REAL
);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(_SCHEMA)
    await _migrate(db)


async def _migrate(db: aiosqlite.Connection) -> None:
    """
    Bring a file created before a column existed up to date, in place --
    CREATE TABLE IF NOT EXISTS above is a no-op against an already-existing
    table, so a new column needs its own ALTER TABLE, run once per db file.
    """

    cursor = await db.execute("PRAGMA table_info(episodic_log)")
    columns = {row[1] for row in await cursor.fetchall()}

    if "modality" not in columns:
        await db.execute(
            "ALTER TABLE episodic_log ADD COLUMN modality TEXT NOT NULL DEFAULT 'text'"
        )
        await db.commit()

    if "action_points" not in columns:
        await db.execute("ALTER TABLE episodic_log ADD COLUMN action_points TEXT")
        await db.commit()

    snapshot_cursor = await db.execute("PRAGMA table_info(memory_snapshot)")
    snapshot_columns = {row[1] for row in await snapshot_cursor.fetchall()}

    if "recent_actions" not in snapshot_columns:
        await db.execute(
            "ALTER TABLE memory_snapshot ADD COLUMN recent_actions "
            "TEXT NOT NULL DEFAULT '{}'"
        )
        await db.execute(
            "ALTER TABLE memory_snapshot ADD COLUMN recent_action_at "
            "TEXT NOT NULL DEFAULT '{}'"
        )
        await db.commit()

    if "full_context_sent_at" not in snapshot_columns:
        await db.execute(
            "ALTER TABLE memory_snapshot ADD COLUMN full_context_sent_at REAL"
        )
        await db.commit()

    if "action_points" not in snapshot_columns:
        await db.execute(
            "ALTER TABLE memory_snapshot ADD COLUMN action_points "
            "TEXT NOT NULL DEFAULT ''"
        )
        await db.commit()


def _pack_embedding(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _unpack_embedding(blob: bytes) -> np.ndarray:
    # Not .tolist(): rank_by_similarity stacks these directly, and
    # converting to a Python list here only to convert back to numpy
    # there measured ~25x slower at scale for no benefit.
    return np.frombuffer(blob, dtype=np.float32)


class SqliteEpisodicStore:
    """Satisfies memory.ports.EpisodicStore against a SQLite file."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._embedder = embedder

    async def log(self, record: EpisodicRecord) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            cursor = await db.execute(
                """
                INSERT INTO episodic_log (
                    timestamp, conversation_id, intervention_path, speaker,
                    content, modality, trigger_variables, strategy_id,
                    prompt_fragment, assistant_output, history_summary,
                    participant_roles_summary, action_points
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp,
                    record.conversation_id,
                    record.intervention_path,
                    record.speaker,
                    record.content,
                    record.modality,
                    json.dumps(record.trigger_variables),
                    record.strategy_id,
                    record.prompt_fragment,
                    record.assistant_output,
                    record.history_summary,
                    record.participant_roles_summary,
                    record.action_points,
                ),
            )
            episodic_log_id = cursor.lastrowid
            await db.commit()

        if self._embedder is None or not record.content:
            return

        # Embedded outside the connection above (and in a fresh one
        # below): holding a SQLite connection open across a network call
        # would sit on the file lock for however long that call takes.
        try:
            embedding = await self._embedder.embed(record.content)
        except Exception:
            logger.warning(
                "Could not embed a logged turn for %s; it won't be "
                "findable by 'remember' until re-embedded",
                record.conversation_id,
                exc_info=True,
            )
            return

        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute(
                """
                INSERT INTO episodic_embedding (
                    episodic_log_id, conversation_id, embedding
                ) VALUES (?, ?, ?)
                """,
                (
                    episodic_log_id,
                    record.conversation_id,
                    _pack_embedding(embedding),
                ),
            )
            await db.commit()

    async def search_similar(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int,
    ) -> list[EpisodicRecord]:
        if self._embedder is None:
            raise RuntimeError(
                "SqliteEpisodicStore has no embedder configured; "
                "search_similar needs one"
            )

        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            cursor = await db.execute(
                """
                SELECT episodic_log_id, embedding FROM episodic_embedding
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            )
            rows = await cursor.fetchall()

        if not rows:
            return []

        query_vector = await self._embedder.embed(query)
        candidate_ids = [row[0] for row in rows]
        candidate_vectors = [_unpack_embedding(row[1]) for row in rows]

        ranked = rank_by_similarity(query_vector, candidate_vectors)
        top_ids = [candidate_ids[i] for i in ranked[:limit]]

        placeholders = ",".join("?" * len(top_ids))
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM episodic_log WHERE id IN ({placeholders})",
                top_ids,
            )
            by_id = {row["id"]: row for row in await cursor.fetchall()}

        # top_ids' order (most similar first), not by_id's arbitrary one.
        return [
            EpisodicRecord(
                timestamp=by_id[i]["timestamp"],
                conversation_id=by_id[i]["conversation_id"],
                intervention_path=by_id[i]["intervention_path"],
                speaker=by_id[i]["speaker"],
                content=by_id[i]["content"],
                modality=by_id[i]["modality"],
                trigger_variables=json.loads(by_id[i]["trigger_variables"]),
                strategy_id=by_id[i]["strategy_id"],
                prompt_fragment=by_id[i]["prompt_fragment"],
                assistant_output=by_id[i]["assistant_output"],
                history_summary=by_id[i]["history_summary"],
                participant_roles_summary=by_id[i]["participant_roles_summary"],
                action_points=by_id[i]["action_points"],
            )
            for i in top_ids
        ]

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
                modality=row["modality"],
                trigger_variables=json.loads(row["trigger_variables"]),
                strategy_id=row["strategy_id"],
                prompt_fragment=row["prompt_fragment"],
                assistant_output=row["assistant_output"],
                history_summary=row["history_summary"],
                participant_roles_summary=row["participant_roles_summary"],
                action_points=row["action_points"],
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
            action_points=row["action_points"],
            history_tail=[
                StoredUtterance(**item)
                for item in json.loads(row["history_tail"])
            ],
            total_message_count=row["total_message_count"],
            updated_at=row["updated_at"],
            recent_actions=json.loads(row["recent_actions"]),
            recent_action_at=json.loads(row["recent_action_at"]),
            full_context_sent_at=row["full_context_sent_at"],
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
                    action_points, history_tail, total_message_count, updated_at,
                    recent_actions, recent_action_at, full_context_sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    history_summary = excluded.history_summary,
                    participant_roles_summary = excluded.participant_roles_summary,
                    action_points = excluded.action_points,
                    history_tail = excluded.history_tail,
                    total_message_count = excluded.total_message_count,
                    updated_at = excluded.updated_at,
                    recent_actions = excluded.recent_actions,
                    recent_action_at = excluded.recent_action_at,
                    full_context_sent_at = excluded.full_context_sent_at
                """,
                (
                    conversation_id,
                    snapshot.history_summary,
                    snapshot.participant_roles_summary,
                    snapshot.action_points,
                    json.dumps(
                        [utterance.model_dump() for utterance in snapshot.history_tail]
                    ),
                    snapshot.total_message_count,
                    snapshot.updated_at,
                    json.dumps(snapshot.recent_actions),
                    json.dumps(snapshot.recent_action_at),
                    snapshot.full_context_sent_at,
                ),
            )
            await db.commit()
