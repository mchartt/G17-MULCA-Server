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
from .intervention_agent import DEFAULT_MODEL as DEFAULT_INTERVENTION_MODEL
from .intervention_agent import InterventionAgentOpenAI
from .main_agent import DEFAULT_REALTIME_MODEL
from .main_agent import MainAgentOpenAI


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
        # Overridable (e.g. to compare cost/quality against gpt-realtime-mini)
        # without touching either agent's own hardcoded default.
        main_model: str = DEFAULT_REALTIME_MODEL,
        intervention_model: str = DEFAULT_INTERVENTION_MODEL,
    ) -> None:
        self._client = client
        self._strategies = strategies
        self._main_persona = main_persona
        self._intervention_persona = intervention_persona
        self._task_knowledge = task_knowledge
        self._main_model = main_model
        self._intervention_model = intervention_model

    def build_main(
        self,
        options: MainAgentOptions,
    ) -> MainAgent:
        return MainAgentOpenAI(
            options=options,
            client=self._client,
            persona=self._main_persona,
            task_knowledge=self._task_knowledge,
            model=self._main_model,
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
                # Per-session, not per-deployment: one factory instance is
                # shared across every conversation, group and DM alike.
                is_dm=options.is_dm,
                model=self._intervention_model,
            )

        return AuxiliaryAgent(options=options)
