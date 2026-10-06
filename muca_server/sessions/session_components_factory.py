from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .session_components import SessionComponents, SessionContext, SessionWorker
from ..agents import AgentFactory, Conversation, MainAgentOptions
from ..agents.conversation import DEFAULT_SILENCE_TIMEOUT_MS
from ..cognition import CognitionPolicy

if TYPE_CHECKING:
    from ..agents.openai.model_settings import ModelSettings

PolicyFactory = Callable[[SessionContext], CognitionPolicy]


class DefaultSessionComponentsFactory:
    """
    Builds one session's components.

    Everything a deployment chooses — which agents, which cognition, how the
    main agent is configured — arrives here, so nothing below has to know.
    """

    def __init__(
        self,
        *,
        agent_factory: AgentFactory | None = None,
        policy_factory: PolicyFactory | None = None,
        main_agent_options: MainAgentOptions | None = None,
        silence_timeout_ms: int = DEFAULT_SILENCE_TIMEOUT_MS,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self.model_settings = model_settings
        self.agent_factory = agent_factory
        self.policy_factory = policy_factory
        self.main_agent_options = (
            main_agent_options or MainAgentOptions()
        )
        self.silence_timeout_ms = silence_timeout_ms

    async def create(
        self,
        context: SessionContext,
    ) -> SessionComponents:
        conversation = await self.create_conversation(context)

        workers = await self.create_workers(
            context=context,
            conversation=conversation,
        )

        return SessionComponents(
            agent_router=conversation,
            workers=workers,
        )

    async def create_conversation(
        self,
        context: SessionContext,
    ) -> Conversation:
        policy = (
            self.policy_factory(context)
            if self.policy_factory is not None
            else None
        )

        return Conversation(
            state=context.state,
            outbound_queue=context.outbound_queue,
            main_agent_options=self.main_agent_options,
            agent_factory=self.agent_factory,
            policy=policy,
            silence_timeout_ms=self.silence_timeout_ms,
            model_settings=self.model_settings,
        )

    async def create_workers(
        self,
        *,
        context: SessionContext,
        conversation: Conversation,
    ) -> list[SessionWorker]:
        return [conversation]
