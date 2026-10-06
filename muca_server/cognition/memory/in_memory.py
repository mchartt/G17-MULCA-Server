from __future__ import annotations

from .ports import Embedder
from .records import EpisodicRecord, MemorySnapshot
from .similarity import rank_by_similarity


class InMemoryEpisodicStore:
    """Remembers for as long as the process runs."""

    def __init__(self, *, embedder: Embedder | None = None) -> None:
        self._records: dict[str, list[EpisodicRecord]] = {}
        # Parallel to _records, one entry each -- None for a record with
        # no embedder configured or nothing to embed (empty content, e.g.
        # a management_cycle row), so the two lists always line up by index.
        self._embeddings: dict[str, list[list[float] | None]] = {}
        self._embedder = embedder

    async def log(self, record: EpisodicRecord) -> None:
        self._records.setdefault(
            record.conversation_id,
            [],
        ).append(record)

        embedding = None
        if self._embedder is not None and record.content:
            embedding = await self._embedder.embed(record.content)
        self._embeddings.setdefault(record.conversation_id, []).append(embedding)

    async def recall_recent(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[EpisodicRecord]:
        records = [
            record
            for record in self._records.get(conversation_id, [])
            # Management-cycle rows hold summaries, not something anybody
            # said, so they are no use as raw material for an answer.
            # Filtered before the window, so a window of N really is N.
            if record.intervention_path != "management_cycle"
        ]

        return records[-limit:]

    async def search_similar(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int,
    ) -> list[EpisodicRecord]:
        if self._embedder is None:
            raise RuntimeError(
                "InMemoryEpisodicStore has no embedder configured; "
                "search_similar needs one"
            )

        records = self._records.get(conversation_id, [])
        embeddings = self._embeddings.get(conversation_id, [])

        candidates = [
            (record, embedding)
            for record, embedding in zip(records, embeddings)
            if embedding is not None
        ]
        if not candidates:
            return []

        query_vector = await self._embedder.embed(query)
        ranked = rank_by_similarity(
            query_vector,
            [embedding for _, embedding in candidates],
        )

        return [candidates[i][0] for i in ranked[:limit]]


class InMemorySnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, MemorySnapshot] = {}

    async def load(
        self,
        conversation_id: str,
    ) -> MemorySnapshot | None:
        return self._snapshots.get(conversation_id)

    async def save(
        self,
        conversation_id: str,
        snapshot: MemorySnapshot,
    ) -> None:
        self._snapshots[conversation_id] = snapshot
