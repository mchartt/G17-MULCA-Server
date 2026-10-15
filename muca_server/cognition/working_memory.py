"""
What the assistant is holding in mind about one conversation.

Ownership is enforced by which methods exist rather than by convention:
the policy writes the shared and forced fields, while `latest_strategy_id`
and `latest_prompt_fragment` are authored by an observer and only relayed
here verbatim. A forced directive and a selected strategy are mutually
exclusive per turn by construction, so whatever builds the reply prompt
never has to reconcile both at once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .ports import Utterance
from .strategies import Strategy

# On top of cooldown_messages, for the same strategies -- voice naturally
# produces more completed turns per real second than text (people talk
# faster than they type), so the same message-count window elapses in much
# less wall-clock time there. Found live: participation_equality refiring
# within a voice call far more often than it felt like it should, even
# though message counting itself is correct and identical for both
# modalities (one increment per completed turn, text or voice, via the one
# shared on_utterance path). A flat constant, not per-strategy, for now.
SECONDARY_COOLDOWN_SECONDS = 60.0


@dataclass
class WorkingMemory:
    conversation_id: str

    # Set once, from config.
    assistant_id: int = 0

    # Per turn.
    history: list[Utterance] = field(default_factory=list)
    trigger_variables: dict = field(default_factory=dict)

    # Authored by an observer, relayed here unedited.
    latest_strategy_id: str | None = None
    latest_prompt_fragment: str | None = None

    # A directive that bypassed the observers entirely.
    forced_context: str | None = None

    # Long-term memory pulled in for this turn only.
    retrieved_context: str | None = None

    # The days (first, last) this turn's history question is about; None
    # for the whole history. This turn only.
    history_period: tuple[str, str] | None = None

    # Live web search results pulled in for this turn only -- kept
    # separate from retrieved_context above, not reused for it: that one
    # is specifically labeled "Retrieved conversation history" in the
    # rendered prompt, which would be a wrong, confusing label for this.
    web_search_context: str | None = None

    # {strategy_id: total_message_count when it last fired}.
    recent_actions: dict[str, int] = field(default_factory=dict)
    # {strategy_id: time.time() when it last fired} -- the secondary,
    # wall-clock half of cooldown (SECONDARY_COOLDOWN_SECONDS).
    recent_action_at: dict[str, float] = field(default_factory=dict)

    # Monotonic: survives the history truncation below.
    total_message_count: int = 0
    # The management cycle's running summary, from before day summaries
    # (docs/MEMORY_DESIGN.md): no longer written, only carried over in the
    # snapshot (so an older server version still finds it) and shown
    # until the first overview exists.
    history_summary: str = ""
    participant_roles_summary: str = ""
    action_points: str = ""

    def append_history(self, utterance: Utterance) -> None:
        self.history.append(utterance)

    def increment_message_count(self) -> int:
        self.total_message_count += 1
        return self.total_message_count

    def trim_history(self, *, tail_size: int) -> None:
        """Keep the recent window: the last tail_size messages, verbatim."""

        self.history = self.history[-tail_size:]

    def record_action(
        self,
        strategy_id: str,
        message_count: int,
        *,
        now: float | None = None,
    ) -> None:
        self.recent_actions[strategy_id] = message_count
        self.recent_action_at[strategy_id] = time.time() if now is None else now

    def strategy_in_cooldown(
        self,
        strategy: Strategy,
        *,
        now: float | None = None,
    ) -> bool:
        """
        Whether this strategy fired too recently to fire again.

        Two independent gates, either one enough to veto: the message-count
        one (measured against `total_message_count`, never against
        `trigger_variables["message_count"]` -- the latter is the length of
        the history, which the recent window truncates, so the gap could
        reset or go negative and leave a strategy stuck in cooldown
        forever), and a flat wall-clock one (SECONDARY_COOLDOWN_SECONDS) on
        top of it. The message-count gate alone means the same "N turns"
        contract permits much more frequent real-time firing in voice than
        in text, since people talk faster than they type -- the wall-clock
        gate is what actually bounds that.
        """

        if not strategy.cooldown_messages:
            return False

        last_fired = self.recent_actions.get(strategy.id)
        if last_fired is None:
            return False

        if (self.total_message_count - last_fired) < strategy.cooldown_messages:
            return True

        last_fired_at = self.recent_action_at.get(strategy.id)
        if last_fired_at is None:
            return False

        now = time.time() if now is None else now
        return (now - last_fired_at) < SECONDARY_COOLDOWN_SECONDS

    def set_trigger_variables(self, variables: dict) -> None:
        self.trigger_variables = variables

    def set_intervention_result(
        self,
        strategy_id: str | None,
        prompt_fragment: str | None,
    ) -> None:
        self.latest_strategy_id = strategy_id
        self.latest_prompt_fragment = prompt_fragment
        self.forced_context = None

    def set_forced_context(self, context: str) -> None:
        self.forced_context = context
        self.latest_strategy_id = None
        self.latest_prompt_fragment = None

    def set_retrieved_context(self, text: str | None) -> None:
        self.retrieved_context = text

    def set_web_search_context(self, text: str | None) -> None:
        self.web_search_context = text

    def clear_turn_output(self) -> None:
        """
        Forget this turn's directive.

        Without it, a turn where nothing fires would replay the previous
        turn's strategy.
        """

        self.latest_strategy_id = None
        self.latest_prompt_fragment = None
        self.forced_context = None
        self.retrieved_context = None
        self.web_search_context = None
        self.history_period = None

    def transcript(
        self,
        limit: int | None = None,
        *,
        exclude_assistant: bool = False,
    ) -> str:
        """
        exclude_assistant=True is for the summarizer specifically (mirrors
        compute_trigger_variables already excluding the assistant from
        participation stats): MULCA is not a participant to describe roles
        for, and its own turns are unaffected by anything a summary skips
        forgetting -- they are never truncated out of the live session the
        way a human's older messages functionally are. The main LLM's own
        "recent messages" block still wants exclude_assistant=False, since
        it should see what it itself just said for continuity.

        limit defaults to None (render all of self.history), not an
        independent cap -- self.history is already the recent window
        (trim_history).
        """

        history = self.history if limit is None else self.history[-limit:]
        return "\n".join(
            f"{utterance.label()}: {utterance.content}"
            for utterance in history
            if not exclude_assistant or utterance.speaker_id != self.assistant_id
        )
