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

import csv
import io
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
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

from .records import EpisodicRecord, MemorySnapshot, MemorySummary, StoredUtterance

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

-- Voice calls starting/ending, wake sessions starting/ending and why: for
-- analysis only. Its own table, not rows in episodic_log, so nothing that
-- reads memory (recall, "remember", summaries) ever sees one -- including
-- an older server version, which doesn't know this table exists.
CREATE TABLE IF NOT EXISTS session_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    conversation_id TEXT NOT NULL,
    event TEXT NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);

-- Day summaries, "today so far" and the overview (docs/MEMORY_DESIGN.md):
-- generated from episodic_log, never from a previous summary. Its own
-- table, so episodic_log and memory_snapshot keep exactly the shape older
-- server versions read.
CREATE TABLE IF NOT EXISTS memory_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    content TEXT NOT NULL,
    through_ts REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_summary_conversation
    ON memory_summary (conversation_id, kind, day);

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


def _row_to_record(row) -> EpisodicRecord:
    return EpisodicRecord(
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


def _pack_embedding(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _unpack_embedding(blob: bytes) -> np.ndarray:
    # Not .tolist(): rank_by_similarity stacks these directly, and
    # converting to a Python list here only to convert back to numpy
    # there measured ~25x slower at scale for no benefit.
    return np.frombuffer(blob, dtype=np.float32)


def export_episodic_csv(db_path: str | Path, *, since: float | None = None) -> str:
    """
    One conversation's episodic_log as CSV, oldest first, for analysis
    (/mulca export), with its session events (session_event table)
    interleaved by time as rows whose intervention_path is
    "session_event": content is "<event>: <detail>", trigger_variables the
    event's data. Read-only: opened with mode=ro and no schema check, so
    exporting never migrates or otherwise touches an existing file --
    whatever columns an older file has are exported as they are, and a
    file without session events simply has none. Embedding vectors are
    left out (binary, and in their own table anyway).
    """

    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        since = since or 0
        turns = db.execute(
            "SELECT * FROM episodic_log WHERE timestamp >= ? ORDER BY timestamp, id",
            (since,),
        ).fetchall()
        columns = [
            row[1] for row in db.execute("PRAGMA table_info(episodic_log)")
        ]
        has_events = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_event'"
        ).fetchone()
        events = (
            db.execute(
                "SELECT * FROM session_event WHERE timestamp >= ? ORDER BY timestamp, id",
                (since,),
            ).fetchall()
            if has_events
            else []
        )

    merged = [(row["timestamp"], 0, dict(row)) for row in turns]
    for event in events:
        row = {column: "" for column in columns}
        row.update(
            id=f"event-{event['id']}",
            timestamp=event["timestamp"],
            conversation_id=event["conversation_id"],
            intervention_path="session_event",
            speaker=event["speaker"],
            content=(
                f"{event['event']}: {event['detail']}"
                if event["detail"]
                else event["event"]
            ),
            trigger_variables=event["data"],
        )
        if "modality" in row:
            row["modality"] = "audio"
        merged.append((event["timestamp"], 1, row))
    merged.sort(key=lambda item: (item[0], item[1]))

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time_utc", *columns])
    for timestamp, _, row in merged:
        time_utc = datetime.fromtimestamp(timestamp, timezone.utc)
        writer.writerow(
            [time_utc.strftime("%Y-%m-%d %H:%M:%S"), *(row.get(c, "") for c in columns)]
        )
    return out.getvalue()


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

    async def log_session_event(
        self,
        conversation_id: str,
        event: str,
        *,
        timestamp: float,
        speaker: str = "",
        detail: str = "",
        data: dict | None = None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute(
                """
                INSERT INTO session_event (
                    timestamp, conversation_id, event, speaker, detail, data
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    conversation_id,
                    event,
                    speaker,
                    detail,
                    json.dumps(data or {}),
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
        return [_row_to_record(by_id[i]) for i in top_ids]

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

        return [_row_to_record(row) for row in reversed(rows)]

    async def records_between(
        self,
        conversation_id: str,
        start: float,
        end: float,
    ) -> list[EpisodicRecord]:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM episodic_log
                WHERE conversation_id = ? AND timestamp >= ? AND timestamp < ?
                    AND intervention_path NOT IN ('management_cycle', 'untranscribed')
                ORDER BY timestamp, id
                """,
                (conversation_id, start, end),
            )
            return [_row_to_record(row) for row in await cursor.fetchall()]

    async def spoken_timestamps(self, conversation_id: str) -> list[float]:
        """When anything was said -- which days have something to summarize."""

        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            cursor = await db.execute(
                """
                SELECT timestamp FROM episodic_log
                WHERE conversation_id = ? AND content != ''
                    AND intervention_path NOT IN ('management_cycle', 'untranscribed')
                """,
                (conversation_id,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def call_ends_between(
        self,
        conversation_id: str,
        start: float,
        end: float,
    ) -> list[float]:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            cursor = await db.execute(
                """
                SELECT timestamp FROM session_event
                WHERE conversation_id = ? AND event = 'voice_call_ended'
                    AND timestamp >= ? AND timestamp < ?
                """,
                (conversation_id, start, end),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def save_summary(self, conversation_id: str, summary: MemorySummary) -> None:
        """Replaces any summary of the same kind for the same day."""

        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute(
                "DELETE FROM memory_summary"
                " WHERE conversation_id = ? AND kind = ? AND day = ?",
                (conversation_id, summary.kind, summary.day),
            )
            await db.execute(
                """
                INSERT INTO memory_summary (
                    conversation_id, kind, day, content, through_ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    summary.kind,
                    summary.day,
                    json.dumps(summary.content),
                    summary.through_ts,
                    summary.created_at,
                ),
            )
            await db.commit()

    async def delete_summaries(self, conversation_id: str, kind: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute(
                "DELETE FROM memory_summary WHERE conversation_id = ? AND kind = ?",
                (conversation_id, kind),
            )
            await db.commit()

    async def load_summaries(self, conversation_id: str) -> list[MemorySummary]:
        """Oldest day first."""

        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            cursor = await db.execute(
                """
                SELECT kind, day, content, through_ts, created_at FROM memory_summary
                WHERE conversation_id = ? ORDER BY day, id
                """,
                (conversation_id,),
            )
            return [
                MemorySummary(
                    kind=row[0],
                    day=row[1],
                    content=json.loads(row[2]),
                    through_ts=row[3],
                    created_at=row[4],
                )
                for row in await cursor.fetchall()
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
