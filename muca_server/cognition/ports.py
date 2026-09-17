"""
The seam between a conversation and whatever decides what it should say.

Deliberately imports nothing else from the project: the agents layer depends
on these names, and the rest of `cognition` depends on the agents layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Utterance:
    """One complete thing somebody said."""

    speaker_id: int
    speaker_name: str
    content: str
    timestamp_ms: int
    modality: Literal["text", "audio"] = "text"

    def label(self) -> str:
        return self.speaker_name or f"User {self.speaker_id}"


@dataclass(frozen=True, slots=True)
class InterventionSignal:
    """An observer's judgement that the assistant should speak now."""

    strategy_id: str
    prompt_fragment: str
    needs_history: bool = False
    needs_web_search: bool = False


class ConversationPort(Protocol):
    """Interface which defines what cognition can ask of its conversation."""

    @property
    def conversation_id(self) -> str:
        ...

    @property
    def is_private(self) -> bool:
        """A private conversation is never written to long-term memory."""
        ...

    @property
    def linked_conversation_id(self) -> str | None:
        """
        For a DM: the group conversation_id it's allowed one-way read access
        to (its sender is a recognized participant of that instance), or
        None if unrecognized. Meaningless when is_private is False.
        """
        ...

    def participants(self) -> dict[int, str]:
        ...

    async def instruct_main_agent(self, directive: str) -> None:
        """Tell the main agent to respond, with guidance on why and roughly what."""
        ...

    async def tell_observers(self, text: str) -> None:
        ...


class Summarizer(Protocol):
    """
    Interface which defines how a conversation's history gets condensed.

    Kept separate from CognitionPolicy because it is the one piece of the
    pipeline that has to call a model: an implementation lives beside a
    provider agent, not in cognition itself.
    """

    async def summarize(
        self,
        *,
        transcript: str,
        previous_summary: str,
        previous_roles: str,
    ) -> tuple[str, str]:
        """Returns (history_summary, participant_roles_summary)."""
        ...


class CognitionPolicy(Protocol):
    """
    Interface which defines whether and what the assistant says.

    A conversation drives turn taking and owns the agents; this decides what
    to do about any of it.
    """

    async def start(self, port: ConversationPort) -> None:
        ...

    async def close(self) -> None:
        ...

    async def on_utterance(self, utterance: Utterance) -> bool:
        """
        Handle somebody speaking.

        Returns whether the observers should now be asked to judge whether to
        intervene. Returning False means this policy already decided.
        """
        ...

    async def on_intervention_signal(
        self,
        agent_guid: UUID,
        signal: InterventionSignal | None,
    ) -> None:
        """`None` means the observer judged that nothing should be said."""
        ...

    async def on_assistant_output(
        self,
        text: str,
        *,
        interrupted: bool,
    ) -> None:
        ...
