from __future__ import annotations

from .records import EpisodicRecord, MemorySnapshot


class InMemoryEpisodicStore:
    """Remembers for as long as the process runs."""

    def __init__(self) -> None:
        self._records: dict[str, list[EpisodicRecord]] = {}

    async def log(self, record: EpisodicRecord) -> None:
        self._records.setdefault(
            record.conversation_id,
            [],
        ).append(record)

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
