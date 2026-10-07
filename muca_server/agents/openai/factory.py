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
        main_knowledge: str = "",
    ) -> None:
        self._client = client
        self._models = models
        # Course material for the main agent only: the observer judges
        # whether to speak, it doesn't answer questions about the course.
        self._main_knowledge = main_knowledge
        self._strategies = strategies
        self._main_persona = main_persona
        self._intervention_persona = intervention_persona
        self._task_knowledge = task_knowledge

    def build_main(
        self,
        options: MainAgentOptions,
    ) -> MainAgent:
        return MainAgentOpenAI(
            options=options,
            client=self._client,
            persona=self._main_persona,
            task_knowledge=(
                f"{self._task_knowledge}\n\nCourse material:\n{self._main_knowledge}"
                if self._main_knowledge
                else self._task_knowledge
            ),
            model=options.model or self._models.main,
        )

    def build_auxiliary(
        self,
        options: AuxiliaryAgentOptions,
    ) -> AuxiliaryAgent:
        if options.agent_type == INTERVENTION_DETECTION:
            return InterventionAgentOpenAI(
                options=options,
                client=self._client,
                strategies=self._strategies,
                persona=self._intervention_persona,
                task_knowledge=self._task_knowledge,
                model=options.model or self._models.intervention,
                # Per-session, not per-deployment: one factory instance is
                # shared across every conversation, group and DM alike.
                is_dm=options.is_dm,
            )

        return AuxiliaryAgent(options=options)
