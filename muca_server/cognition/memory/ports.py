from __future__ import annotations

from typing import Protocol

from .records import EpisodicRecord, MemorySnapshot, MemorySummary


class Embedder(Protocol):
    """
    Turns text into a vector for similarity search.

    Provider-specific (e.g. an OpenAI embeddings call) -- implementations
    live outside cognition, same reason Summarizer's real implementation
    lives beside a provider agent rather than here.
    """

    async def embed(self, text: str) -> list[float]:
        ...


class EpisodicStore(Protocol):
    """Interface which defines the long-term record of what happened."""

    async def log(self, record: EpisodicRecord) -> None:
        ...

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
        """
        Something about the session itself (a call, a wake session), kept
        apart from the turns: never recalled, searched or summarized.
        """
        ...

    async def recall_recent(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[EpisodicRecord]:
        """
        The most recent records for one conversation, oldest first.

        A recency window, not a relevance search: it returns what was said
        lately regardless of what is being asked about.
        """
        ...

    async def search_similar(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int,
    ) -> list[EpisodicRecord]:
        """
        The records whose content is most semantically related to `query`,
        ranked by similarity -- not recency. Most-similar first.

        Needs an embedder to have been configured at construction time;
        raises if one wasn't, rather than silently returning nothing.
        """
        ...

    # Long-term memory by day (docs/MEMORY_DESIGN.md).

    async def records_between(
        self,
        conversation_id: str,
        start: float,
        end: float,
    ) -> list[EpisodicRecord]:
        """Everything said in [start, end), oldest first."""
        ...

    async def spoken_timestamps(self, conversation_id: str) -> list[float]:
        ...

    async def call_ends_between(
        self,
        conversation_id: str,
        start: float,
        end: float,
    ) -> list[float]:
        ...

    async def save_summary(
        self,
        conversation_id: str,
        summary: MemorySummary,
    ) -> None:
        ...

    async def delete_summaries(self, conversation_id: str, kind: str) -> None:
        ...

    async def load_summaries(self, conversation_id: str) -> list[MemorySummary]:
        ...


class SnapshotStore(Protocol):
    """Interface which defines how a conversation survives a restart."""

    async def load(
        self,
        conversation_id: str,
    ) -> MemorySnapshot | None:
        ...

    async def save(
        self,
        conversation_id: str,
        snapshot: MemorySnapshot,
    ) -> None:
        ...
