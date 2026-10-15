from __future__ import annotations

from uuid import UUID

from .ports import ConversationPort, InterventionSignal, Utterance


class NoCognition:
    """
    Decides nothing.

    The default, so a conversation with no cognition configured still routes
    audio, tracks turns and asks its observers to judge.
    """

    async def start(self, port: ConversationPort) -> None:
        pass

    async def close(self) -> None:
        pass

    async def on_utterance(self, utterance: Utterance) -> bool:
        return True

    async def on_intervention_signal(
        self,
        agent_guid: UUID,
        signal: InterventionSignal | None,
    ) -> None:
        pass

    async def on_assistant_output(
        self,
        text: str,
        *,
        interrupted: bool,
    ) -> None:
        pass

    async def wait_for_turn_to_finish(self) -> None:
        pass

    async def on_untranscribed_turn(
        self,
        speaker_id: int,
        speaker_name: str,
        timestamp_ms: int,
    ) -> None:
        pass

    def last_turn_strategy(self) -> str | None:
        return None

    async def on_context_full(self) -> None:
        pass

    async def on_session_event(
        self,
        event: str,
        detail: str = "",
        *,
        speaker: str = "",
        data: dict | None = None,
    ) -> None:
        pass

    def observer_catchup_text(self) -> str:
        return ""
