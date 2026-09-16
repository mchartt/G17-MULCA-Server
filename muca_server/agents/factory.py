from __future__ import annotations

from typing import Protocol

from .auxiliary_agent import AuxiliaryAgent
from .main_agent import MainAgent
from .options import AuxiliaryAgentOptions, MainAgentOptions


class AgentFactory(Protocol):
    """
    Interface which defines how a conversation obtains its agents.

    Swapping this is how a provider is chosen: the conversation itself never
    names a provider, and a test can hand it agents that reach nothing.
    """

    def build_main(
        self,
        options: MainAgentOptions,
    ) -> MainAgent:
        ...

    def build_auxiliary(
        self,
        options: AuxiliaryAgentOptions,
    ) -> AuxiliaryAgent:
        ...


class DefaultAgentFactory:
    """Builds agents that reach no provider."""

    def build_main(
        self,
        options: MainAgentOptions,
    ) -> MainAgent:
        return MainAgent(options=options)

    def build_auxiliary(
        self,
        options: AuxiliaryAgentOptions,
    ) -> AuxiliaryAgent:
        return AuxiliaryAgent(options=options)
