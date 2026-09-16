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


class OpenAIAgentFactory:
    """Satisfies agents.factory.AgentFactory structurally."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        strategies: StrategyStore,
        intervention_persona: str,
        task_knowledge: str,
        is_dm: bool = False,
    ) -> None:
        self._client = client
        self._strategies = strategies
        self._intervention_persona = intervention_persona
        self._task_knowledge = task_knowledge
        self._is_dm = is_dm

    def build_main(
        self,
        options: MainAgentOptions,
    ) -> MainAgent:
        return MainAgentOpenAI(options=options, client=self._client)

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
                is_dm=self._is_dm,
            )

        return AuxiliaryAgent(options=options)
