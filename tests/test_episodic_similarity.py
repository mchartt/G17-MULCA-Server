"""
search_similar: the cosine-ranked retrieval "remember" uses, in both the
in-memory and SQLite stores, plus the pure ranking function they share.
No real OpenAI calls -- a deterministic fake embedder stands in.
"""

from muca_server.cognition.memory.in_memory import InMemoryEpisodicStore
from muca_server.cognition.memory.records import EpisodicRecord
from muca_server.cognition.memory.similarity import rank_by_similarity
from muca_server.cognition.memory.sqlite import SqliteEpisodicStore


class FakeEmbedder:
    """
    Deterministic, no real API calls: a bag-of-words vector over a small
    fixed vocabulary, just enough to meaningfully rank similarity in tests
    without depending on a real model's actual embedding space.
    """

    VOCAB = [
        "coffee", "tea", "budget", "deadline", "timeline", "vendor",
        "proposal",
    ]

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        lowered = text.lower()
        return [float(lowered.count(word)) for word in self.VOCAB]


def record(conversation_id="c1", **kwargs) -> EpisodicRecord:
    defaults = dict(
        timestamp=1.0,
        conversation_id=conversation_id,
        intervention_path="none",
    )
    defaults.update(kwargs)
    return EpisodicRecord(**defaults)


# ----------------------------------------------------------------------
# rank_by_similarity
# ----------------------------------------------------------------------


def test_the_closest_vector_ranks_first():
    ranked = rank_by_similarity([1.0, 0.0], [[0.0, 1.0], [1.0, 0.0], [0.9, 0.1]])

    assert ranked[0] == 1


def test_a_zero_vector_never_ranks_as_a_perfect_match():
    ranked = rank_by_similarity([1.0, 0.0], [[0.0, 0.0], [0.5, 0.5]])

    assert ranked[0] == 1


def test_no_candidates_returns_an_empty_list():
    assert rank_by_similarity([1.0, 0.0], []) == []


# ----------------------------------------------------------------------
# InMemoryEpisodicStore.search_similar
# ----------------------------------------------------------------------


async def test_in_memory_search_similar_ranks_by_relevance_not_recency():
    store = InMemoryEpisodicStore(embedder=FakeEmbedder())

    await store.log(record(content="we should finalize the budget by Friday"))
    await store.log(record(content="Ada suggested a better coffee machine"))
    await store.log(record(content="the vendor contract still needs signing"))

    results = await store.search_similar("c1", "coffee", limit=5)

    assert results[0].content == "Ada suggested a better coffee machine"


async def test_in_memory_search_similar_skips_records_with_no_content():
    store = InMemoryEpisodicStore(embedder=FakeEmbedder())

    await store.log(record(content=""))
    await store.log(record(content="Ada suggested a better coffee machine"))

    results = await store.search_similar("c1", "coffee", limit=5)

    assert len(results) == 1


async def test_in_memory_search_similar_is_scoped_to_one_conversation():
    store = InMemoryEpisodicStore(embedder=FakeEmbedder())

    await store.log(record("c1", content="Ada suggested a better coffee machine"))
    await store.log(record("c2", content="a different team's coffee chat"))

    results = await store.search_similar("c1", "coffee", limit=5)

    assert [r.conversation_id for r in results] == ["c1"]


async def test_in_memory_search_similar_without_an_embedder_raises():
    store = InMemoryEpisodicStore()

    try:
        await store.search_similar("c1", "coffee", limit=5)
    except RuntimeError:
        return
    raise AssertionError("expected a RuntimeError")


# ----------------------------------------------------------------------
# SqliteEpisodicStore.search_similar
# ----------------------------------------------------------------------


async def test_sqlite_search_similar_ranks_by_relevance_not_recency(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db", embedder=FakeEmbedder())

    await store.log(record(content="we should finalize the budget by Friday"))
    await store.log(record(content="Ada suggested a better coffee machine"))
    await store.log(record(content="the vendor contract still needs signing"))

    results = await store.search_similar("c1", "coffee", limit=5)

    assert results[0].content == "Ada suggested a better coffee machine"


async def test_sqlite_search_similar_is_scoped_to_one_conversation(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db", embedder=FakeEmbedder())

    await store.log(record("c1", content="Ada suggested a better coffee machine"))
    await store.log(record("c2", content="a different team's coffee chat"))

    results = await store.search_similar("c1", "coffee", limit=5)

    assert [r.conversation_id for r in results] == ["c1"]


async def test_sqlite_search_similar_without_an_embedder_raises(tmp_path):
    store = SqliteEpisodicStore(tmp_path / "test.db")

    try:
        await store.search_similar("c1", "coffee", limit=5)
    except RuntimeError:
        return
    raise AssertionError("expected a RuntimeError")


async def test_sqlite_log_without_an_embedder_still_works(tmp_path):
    # Embedding is optional, additive behavior -- existing callers that
    # never pass an embedder must be entirely unaffected.
    store = SqliteEpisodicStore(tmp_path / "test.db")

    await store.log(record(content="hello"))

    assert [r.content for r in await store.recall_recent("c1", 50)] == ["hello"]


async def test_sqlite_search_similar_survives_a_new_store_instance(tmp_path):
    # The actual point of the SQLite backing: a fresh instance (standing in
    # for a process restart) sees embeddings an earlier one wrote.
    db_path = tmp_path / "test.db"

    writer = SqliteEpisodicStore(db_path, embedder=FakeEmbedder())
    await writer.log(record(content="Ada suggested a better coffee machine"))

    reader = SqliteEpisodicStore(db_path, embedder=FakeEmbedder())
    results = await reader.search_similar("c1", "coffee", limit=5)

    assert [r.content for r in results] == ["Ada suggested a better coffee machine"]


async def test_a_failed_embedding_does_not_stop_the_turn_from_being_logged(
    tmp_path,
):
    class FailingEmbedder:
        async def embed(self, text: str) -> list[float]:
            raise RuntimeError("the embeddings API is down")

    store = SqliteEpisodicStore(tmp_path / "test.db", embedder=FailingEmbedder())

    await store.log(record(content="hello"))

    assert [r.content for r in await store.recall_recent("c1", 50)] == ["hello"]
