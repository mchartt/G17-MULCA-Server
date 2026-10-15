"""
SqliteEpisodicStore/SqliteSnapshotStore -- the DB-backed persistence that
replaces the in-memory stores for real deployments.

Each test uses its own temp file (pytest's tmp_path), never a shared/live DB.
"""

import csv
import io

import aiosqlite

from muca_server.cognition.memory.records import (
    EpisodicRecord,
    MemorySnapshot,
    StoredUtterance,
)
from muca_server.cognition.memory.sqlite import (
    SqliteEpisodicStore,
    SqliteSnapshotStore,
    export_episodic_csv,
)


def record(conversation_id="c1", **kwargs) -> EpisodicRecord:
    defaults = dict(
        timestamp=1.0,
        conversation_id=conversation_id,
        intervention_path="none",
    )
    defaults.update(kwargs)
    return EpisodicRecord(**defaults)


async def test_recall_recent_is_scoped_to_one_conversation(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")

    await store.log(record("c1", content="hello from c1"))
    await store.log(record("c2", content="hello from c2"))

    rows = await store.recall_recent("c1", 50)

    assert [r.content for r in rows] == ["hello from c1"]


async def test_recall_recent_excludes_management_cycle_rows(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")

    await store.log(record(content="a real message"))
    await store.log(
        record(
            intervention_path="management_cycle",
            history_summary="the summary",
            participant_roles_summary="the roles",
            action_points="the action points",
        )
    )

    rows = await store.recall_recent("c1", 50)

    assert [r.content for r in rows] == ["a real message"]


async def test_a_management_cycle_rows_three_summary_fields_round_trip(tmp_path):
    db_path = tmp_path / "test.db"
    store = SqliteEpisodicStore(db_path)

    await store.log(
        record(
            intervention_path="management_cycle",
            history_summary="the summary",
            participant_roles_summary="the roles",
            action_points="the action points",
        )
    )

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM episodic_log")
        [row] = await cursor.fetchall()

    assert row["history_summary"] == "the summary"
    assert row["participant_roles_summary"] == "the roles"
    assert row["action_points"] == "the action points"


async def test_recall_recent_is_chronological_and_limited(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")

    for i in range(5):
        await store.log(record(timestamp=float(i), content=f"message {i}"))

    rows = await store.recall_recent("c1", 3)

    # The most recent 3, but returned oldest-first (a window, not a reversal).
    assert [r.content for r in rows] == ["message 2", "message 3", "message 4"]


async def test_a_logged_record_round_trips_every_field(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")

    await store.log(
        record(
            timestamp=42.5,
            intervention_path="strategy",
            speaker="Ada",
            content="are you there?",
            trigger_variables={"gini_index": 0.4, "message_count": 3},
            strategy_id="respond_when_addressed",
            prompt_fragment="answer them",
            assistant_output="Yes, I'm here.",
        )
    )

    [row] = await store.recall_recent("c1", 50)

    assert row.timestamp == 42.5
    assert row.speaker == "Ada"
    assert row.content == "are you there?"
    assert row.trigger_variables == {"gini_index": 0.4, "message_count": 3}
    assert row.strategy_id == "respond_when_addressed"
    assert row.prompt_fragment == "answer them"
    assert row.assistant_output == "Yes, I'm here."


async def test_recall_recent_is_empty_for_an_unknown_conversation(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")

    assert await store.recall_recent("never-logged", 50) == []


async def test_snapshot_load_is_none_before_anything_is_saved(tmp_path):
    store = SqliteSnapshotStore(tmp_path / "test.db")

    assert await store.load("c1") is None


async def test_snapshot_save_and_load_round_trips_every_field(tmp_path):
    store = SqliteSnapshotStore(tmp_path / "test.db")

    snapshot = MemorySnapshot(
        history_summary="a dense summary",
        participant_roles_summary="Ada: asks good questions",
        action_points="Grace to send the forecast link",
        history_tail=[
            StoredUtterance(
                speaker_id=1,
                speaker_name="Ada",
                content="hi",
                timestamp_ms=123,
                modality="text",
            )
        ],
        total_message_count=17,
        updated_at=999.5,
        recent_actions={"participation_equality_text": 12},
        recent_action_at={"participation_equality_text": 1000.25},
        full_context_sent_at=1000.25,
    )

    await store.save("c1", snapshot)
    loaded = await store.load("c1")

    assert loaded == snapshot


async def test_a_snapshot_file_from_before_cooldown_columns_existed_still_loads(
    tmp_path,
):
    # Simulates a real pre-existing DB file: the memory_snapshot table
    # without recent_actions/recent_action_at, same shape _migrate() has
    # to bring up to date in place.
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE memory_snapshot (
                conversation_id TEXT PRIMARY KEY,
                history_summary TEXT NOT NULL DEFAULT '',
                participant_roles_summary TEXT NOT NULL DEFAULT '',
                history_tail TEXT NOT NULL DEFAULT '[]',
                total_message_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            "INSERT INTO memory_snapshot (conversation_id, total_message_count) "
            "VALUES ('c1', 5)"
        )
        await db.commit()

    loaded = await SqliteSnapshotStore(db_path).load("c1")

    assert loaded.total_message_count == 5
    assert loaded.recent_actions == {}
    assert loaded.full_context_sent_at is None
    assert loaded.recent_action_at == {}
    assert loaded.action_points == ""


async def test_a_snapshot_file_from_before_action_points_existed_still_loads(
    tmp_path,
):
    # Simulates a real pre-existing DB file (post cooldown columns, pre
    # action_points) -- same migration pattern, one column later.
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE memory_snapshot (
                conversation_id TEXT PRIMARY KEY,
                history_summary TEXT NOT NULL DEFAULT '',
                participant_roles_summary TEXT NOT NULL DEFAULT '',
                history_tail TEXT NOT NULL DEFAULT '[]',
                total_message_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                recent_actions TEXT NOT NULL DEFAULT '{}',
                recent_action_at TEXT NOT NULL DEFAULT '{}',
                full_context_sent_at REAL
            )
            """
        )
        await db.execute(
            "INSERT INTO memory_snapshot (conversation_id, total_message_count) "
            "VALUES ('c1', 5)"
        )
        await db.commit()

    loaded = await SqliteSnapshotStore(db_path).load("c1")

    assert loaded.total_message_count == 5
    assert loaded.action_points == ""


async def test_an_episodic_log_file_from_before_action_points_existed_still_loads(
    tmp_path,
):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE episodic_log (
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
                participant_roles_summary TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO episodic_log (timestamp, conversation_id, "
            "intervention_path, content) VALUES (1.0, 'c1', 'none', 'hi')"
        )
        await db.commit()

    [row] = await SqliteEpisodicStore(db_path).recall_recent("c1", 50)

    assert row.content == "hi"
    assert row.action_points is None


async def test_saving_again_overwrites_rather_than_duplicates(tmp_path):
    store = SqliteSnapshotStore(tmp_path / "test.db")

    await store.save("c1", MemorySnapshot(total_message_count=1))
    await store.save("c1", MemorySnapshot(total_message_count=2))

    loaded = await store.load("c1")

    assert loaded.total_message_count == 2


async def test_snapshots_for_different_conversations_do_not_collide(tmp_path):
    store = SqliteSnapshotStore(tmp_path / "test.db")

    await store.save("c1", MemorySnapshot(total_message_count=1))
    await store.save("c2", MemorySnapshot(total_message_count=99))

    assert (await store.load("c1")).total_message_count == 1
    assert (await store.load("c2")).total_message_count == 99


async def test_state_survives_a_new_store_instance_pointed_at_the_same_file(
    tmp_path,
):
    # The actual point of moving off the in-memory stores: a fresh instance
    # (standing in for a process restart) sees what an earlier one wrote.
    db_path = tmp_path / "test.db"

    writer = SqliteSnapshotStore(db_path)
    await writer.save("c1", MemorySnapshot(total_message_count=5))

    reader = SqliteSnapshotStore(db_path)
    loaded = await reader.load("c1")

    assert loaded.total_message_count == 5


async def test_session_events_go_to_their_own_table_never_into_memory(tmp_path):
    class CountingEmbedder:
        def __init__(self):
            self.texts = []

        async def embed(self, text: str) -> list[float]:
            self.texts.append(text)
            return [1.0, 0.0]

    embedder = CountingEmbedder()
    db_path = tmp_path / "test.db"
    store = SqliteEpisodicStore(db_path, embedder=embedder)

    await store.log(record(content="a real message"))
    await store.log_session_event(
        "c1", "wake_ended", timestamp=2.0, detail="silence", data={"duration_s": 42}
    )

    assert [r.content for r in await store.recall_recent("c1", 50)] == ["a real message"]
    assert embedder.texts == ["a real message"]
    async with aiosqlite.connect(db_path) as db:
        paths = await (await db.execute("SELECT intervention_path FROM episodic_log")).fetchall()
        events = await (await db.execute("SELECT event, detail, data FROM session_event")).fetchall()
    # episodic_log only ever holds the row kinds older code knows.
    assert paths == [("none",)]
    assert events == [("wake_ended", "silence", '{"duration_s": 42}')]


async def test_an_old_file_keeps_its_rows_next_to_new_session_events(tmp_path):
    # A file from before action_points existed: its old row is untouched,
    # and a session event can be logged into it and exported with it.
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE episodic_log (
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
            )
            """
        )
        await db.execute(
            "INSERT INTO episodic_log (timestamp, conversation_id, "
            "intervention_path, speaker, content) "
            "VALUES (1.0, 'c1', 'strategy', 'Ada', 'hi mulca')"
        )
        await db.commit()

    store = SqliteEpisodicStore(db_path)
    await store.log_session_event(
        "c1", "voice_call_started", timestamp=2.0, detail="Ada"
    )

    [old] = await store.recall_recent("c1", 50)
    assert (old.speaker, old.content, old.intervention_path) == (
        "Ada",
        "hi mulca",
        "strategy",
    )

    rows = list(csv.DictReader(io.StringIO(export_episodic_csv(db_path))))
    assert [(r["intervention_path"], r["content"]) for r in rows] == [
        ("strategy", "hi mulca"),
        ("session_event", "voice_call_started: Ada"),
    ]
    assert rows[0]["time_utc"] == "1970-01-01 00:00:01"


async def test_export_interleaves_events_with_turns_by_time(tmp_path):
    db_path = tmp_path / "test.db"
    store = SqliteEpisodicStore(db_path)
    await store.log(record(timestamp=1.0, content="first"))
    await store.log(record(timestamp=3.0, content="third"))
    await store.log_session_event(
        "c1", "wake_started", timestamp=2.0, speaker="Ada", data={"x": 1}
    )

    rows = list(csv.DictReader(io.StringIO(export_episodic_csv(db_path))))

    assert [r["content"] for r in rows] == ["first", "wake_started", "third"]
    assert rows[1]["speaker"] == "Ada"
    assert rows[1]["trigger_variables"] == '{"x": 1}'
    assert rows[1]["id"].startswith("event-")


async def test_exporting_never_changes_the_file(tmp_path):
    # Read-only: an old file is exported as it is, not migrated.
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE episodic_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                conversation_id TEXT NOT NULL,
                intervention_path TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            "INSERT INTO episodic_log (timestamp, conversation_id, "
            "intervention_path, content) VALUES (5.0, 'c1', 'none', 'hello')"
        )
        await db.commit()
    before = db_path.read_bytes()

    text = export_episodic_csv(db_path)

    assert db_path.read_bytes() == before
    assert text.splitlines()[0] == (
        "time_utc,id,timestamp,conversation_id,intervention_path,content"
    )


async def test_export_since_leaves_out_older_rows(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")
    await store.log(record(timestamp=10.0, content="old"))
    await store.log(record(timestamp=20.0, content="new"))

    rows = list(
        csv.DictReader(
            io.StringIO(export_episodic_csv(tmp_path / "test.db", since=15.0))
        )
    )

    assert [r["content"] for r in rows] == ["new"]


async def test_summaries_are_saved_replaced_per_day_and_loaded_oldest_first(tmp_path):
    from muca_server.cognition.memory.records import MemorySummary

    store = SqliteEpisodicStore(tmp_path / "test.db")
    for conversation_id, kind, day, narrative in (
        ("c1", "day", "2026-10-14", "b"),
        ("c1", "day", "2026-10-13", "a"),
        ("c1", "day", "2026-10-14", "b2"),
        ("c1", "today", "2026-10-15", "t"),
        ("c2", "day", "2026-10-14", "other"),
    ):
        await store.save_summary(
            conversation_id,
            MemorySummary(kind=kind, day=day, content={"narrative": narrative}),
        )

    loaded = await store.load_summaries("c1")
    assert [(s.kind, s.day, s.content["narrative"]) for s in loaded] == [
        ("day", "2026-10-13", "a"),
        ("day", "2026-10-14", "b2"),
        ("today", "2026-10-15", "t"),
    ]

    await store.delete_summaries("c1", "today")
    assert [s.kind for s in await store.load_summaries("c1")] == ["day", "day"]


async def test_a_days_records_leave_out_noise_and_old_summary_rows(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")
    await store.log(record(timestamp=10.0, content="said"))
    await store.log(record(timestamp=11.0, intervention_path="untranscribed"))
    await store.log(
        record(timestamp=12.0, intervention_path="management_cycle", history_summary="s")
    )
    await store.log(record(timestamp=99.0, content="next day"))
    await store.log_session_event("c1", "voice_call_ended", timestamp=10.5)

    records = await store.records_between("c1", 0.0, 50.0)

    assert [r.content for r in records] == ["said"]
    assert await store.spoken_timestamps("c1") == [10.0, 99.0]
    assert await store.call_ends_between("c1", 0.0, 50.0) == [10.5]


async def test_an_old_file_only_gains_the_summary_table(tmp_path):
    # An older server version keeps working on the same file: its tables
    # are untouched, summaries live in a table it doesn't know about.
    from muca_server.cognition.memory.records import MemorySummary

    db_path = tmp_path / "test.db"
    store = SqliteEpisodicStore(db_path)
    await store.log(record(content="hi"))
    async with aiosqlite.connect(db_path) as db:
        before = await (await db.execute("PRAGMA table_info(episodic_log)")).fetchall()

    await store.save_summary(
        "c1", MemorySummary(kind="overview", day="2026-10-14", content={})
    )

    async with aiosqlite.connect(db_path) as db:
        after = await (await db.execute("PRAGMA table_info(episodic_log)")).fetchall()
        tables = {
            row[0]
            for row in await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert before == after
    assert "memory_summary" in tables
    assert [r.content for r in await store.recall_recent("c1", 50)] == ["hi"]
