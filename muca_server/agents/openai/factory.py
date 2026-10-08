"""
Builds OpenAI-backed agents. The composition root for a real deployment,
the equivalent of MULCA's `run_discord_bot.py` factories.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ...cognition.strategies import StrategyStore
from ..auxiliary_agent import AuxiliaryAgent
from ..main_agent import MainAgent
from ..options import (
    INTERVENTION_DETECTION,
    AuxiliaryAgentOptions,
    MainAgentOptions,
)
from ...usage_report import UsageTracker
from .intervention_agent import InterventionAgentOpenAI
from .main_agent import MainAgentOpenAI
from .model_settings import ModelSettings


class OpenAIAgentFactory:
    """Satisfies agents.factory.AgentFactory structurally."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        strategies: StrategyStore,
        main_persona: str,
        intervention_persona: str,
        task_knowledge: str,
        models: ModelSettings,
        usage_tracker: UsageTracker | None = None,
        non_ambient_strategies: StrategyStore | None = None,
    ) -> None:
        self._client = client
        self._models = models
        self._strategies = strategies
        # A separate catalog for "toggle"/"transcribe" conversations (see
        # run_server.py) -- defaults to the same store when not given
        # (tests that don't care about the distinction), never silently
        # merged into one: an ambient conversation's own InterventionAgentOpenAI
        # must never be built with always_reply in its catalog at all, since
        # that catalog is a structured-output enum the model can otherwise
        # actually select from (see InterventionAgentOpenAI._tool_definition),
        # not just prose it might misjudge.
        self._non_ambient_strategies = non_ambient_strategies or strategies
        self._main_persona = main_persona
        self._intervention_persona = intervention_persona
        self._task_knowledge = task_knowledge
        self._usage_tracker = usage_tracker

    def build_main(
        self,
        options: MainAgentOptions,
    ) -> MainAgent:
        return MainAgentOpenAI(
            options=options,
            client=self._client,
            persona=self._main_persona,
            task_knowledge=self._task_knowledge,
            model=options.model or self._models.main,
            usage_tracker=self._usage_tracker,
        )

    def build_auxiliary(
        self,
        options: AuxiliaryAgentOptions,
    ) -> AuxiliaryAgent:
        if options.agent_type == INTERVENTION_DETECTION:
            strategies = (
                self._non_ambient_strategies
                if options.listening_mode in ("toggle", "transcribe")
                else self._strategies
            )
            return InterventionAgentOpenAI(
                options=options,
                client=self._client,
                strategies=strategies,
                persona=self._intervention_persona,
                task_knowledge=self._task_knowledge,
                model=options.model or self._models.intervention,
                # Per-session, not per-deployment: one factory instance is
                # shared across every conversation, group and DM alike.
                is_dm=options.is_dm,
                usage_tracker=self._usage_tracker,
            )

        return AuxiliaryAgent(options=options)
