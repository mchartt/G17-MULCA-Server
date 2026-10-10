"""
The fixed set of things the assistant knows how to do about a conversation.

Strategies are loaded once and never change at runtime: the observers select
and parameterize from this set, they do not invent new ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class Strategy:
    id: str
    trigger_description: str
    prompt_template: str
    trigger_variables: list[str] = field(default_factory=list)

    # A short, appendable instruction used when this strategy rides along
    # on top of a *different* strategy's directive (see InterventionSignal.
    # flag_offensive_language/nudge_participation), instead of the full
    # prompt_template a standalone firing gets. None for strategies that
    # are never a rider.
    overlay_template: str | None = None

    # e.g. {"variable": "gini_index", "min": 0.3}. Given to the observers as a
    # concrete number to reason against rather than vague language; the firing
    # decision is still theirs.
    trigger_threshold: dict | None = None

    # How many messages must pass after this fires before it may fire again.
    # Enforced deterministically, never left to a model to remember.
    cooldown_messages: int = 0

    # Which modality a turn must have arrived in for this strategy to be
    # eligible at all -- enforced as a hard veto in
    # CoalaPolicy.on_intervention_signal, the same deterministic way
    # cooldown is, not left to the observer to remember. Defaults to text
    # only: every strategy predates voice and was written assuming a typed
    # channel, so an existing strategy's YAML silently keeps that behavior
    # unless it opts into "audio" explicitly.
    modalities: frozenset[str] = field(default_factory=lambda: frozenset({"text"}))


class StrategyStore:
    def __init__(
        self,
        strategies: dict[str, Strategy],
    ) -> None:
        self._strategies = strategies

    @classmethod
    def load_from_dir(
        cls,
        directory: str | Path,
        *,
        active_ids: list[str] | None = None,
    ) -> "StrategyStore":
        """
        Load the pool, optionally keeping only the chosen subset.

        Which strategies are active is a startup choice made alongside the
        persona and task, not derived from either. An unknown id is a config
        mistake worth failing on rather than ignoring.
        """

        strategies: dict[str, Strategy] = {}

        for path in sorted(Path(directory).glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            strategy = Strategy(
                id=data["id"],
                trigger_description=data["trigger_description"],
                prompt_template=data["prompt_template"],
                trigger_variables=data.get("trigger_variables", []),
                trigger_threshold=data.get("trigger_threshold"),
                cooldown_messages=data.get("cooldown_messages", 0),
                modalities=frozenset(data.get("modalities", ["text"])),
                overlay_template=data.get("overlay_template"),
            )
            strategies[strategy.id] = strategy

        if active_ids is not None:
            missing = set(active_ids) - strategies.keys()
            if missing:
                raise ValueError(
                    "active_ids references unknown strategies: "
                    f"{sorted(missing)}"
                )
            strategies = {
                sid: strategies[sid] for sid in active_ids
            }

        return cls(strategies)

    def get(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    def all(self) -> list[Strategy]:
        return list(self._strategies.values())

    def ids(self) -> list[str]:
        return list(self._strategies)

    def catalog_for_prompt(self) -> str:
        """
        The catalog as the observers see it.

        Deliberately without `prompt_template`: they decide *whether* a
        strategy applies, not how the reply is worded.
        """

        lines = []

        for strategy in self.all():
            threshold_line = ""
            if strategy.trigger_threshold:
                threshold = strategy.trigger_threshold
                threshold_line = (
                    f"\n  threshold: only fire if {threshold['variable']} "
                    f">= {threshold['min']}"
                )

            cooldown_line = (
                "\n  cooldown: do not select again within "
                f"{strategy.cooldown_messages} messages of firing"
                if strategy.cooldown_messages
                else ""
            )

            variables_line = (
                ", ".join(strategy.trigger_variables)
                if strategy.trigger_variables
                else "none -- judge this directly from the conversation content"
            )

            lines.append(
                f"- id: {strategy.id}\n"
                f"  trigger: {strategy.trigger_description}\n"
                f"  variables_used: {variables_line}"
                f"{threshold_line}"
                f"{cooldown_line}"
            )

        return "\n".join(lines)
