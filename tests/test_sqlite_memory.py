"""
SqliteEpisodicStore/SqliteSnapshotStore -- the DB-backed persistence that
replaces the in-memory stores for real deployments.

Each test uses its own temp file (pytest's tmp_path), never a shared/live DB.
"""

import aiosqlite

from muca_server.cognition.memory.records import (
    EpisodicRecord,
    MemorySnapshot,
    StoredUtterance,
)
from muca_server.cognition.memory.sqlite import (
    SqliteEpisodicStore,
    SqliteSnapshotStore,
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
