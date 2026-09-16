from __future__ import annotations

from typing import Protocol

from .records import EpisodicRecord, MemorySnapshot


class EpisodicStore(Protocol):
    """Interface which defines the long-term record of what happened."""

    async def log(self, record: EpisodicRecord) -> None:
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
