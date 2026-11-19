from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from .session_components import SessionComponents, SessionContext, SessionWorker
from ..agents import AgentFactory, Conversation, MainAgentOptions
from ..agents.conversation import DEFAULT_SILENCE_TIMEOUT_MS
from ..cognition import CognitionPolicy

if TYPE_CHECKING:
    from ..agents.openai.model_settings import ModelSettings
    from ..usage_report import UsageTracker

PolicyFactory = Callable[[SessionContext], CognitionPolicy]

# Group Coordinator: given the session's Conversation and outbound queue,
# returns the router the dispatcher talks to (a CoordinatorRouter wrapping
# the Conversation). None, the default, means no coordinator: the
# Conversation is the router, exactly as before.
CoordinatorFactory = Callable[[Conversation, object], object]


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
        usage_tracker: UsageTracker | None = None,
        listening_mode: Literal["ambient", "toggle", "transcribe", "wake"] = "ambient",
        coordinator_factory: CoordinatorFactory | None = None,
    ) -> None:
        self.coordinator_factory = coordinator_factory
        self.model_settings = model_settings
        self.usage_tracker = usage_tracker
        self.agent_factory = agent_factory
        self.policy_factory = policy_factory
        self.main_agent_options = (
            main_agent_options or MainAgentOptions()
        )
        self.silence_timeout_ms = silence_timeout_ms
        self.listening_mode = listening_mode

    async def create(
        self,
        context: SessionContext,
    ) -> SessionComponents:
        conversation = await self.create_conversation(context)

        workers = await self.create_workers(
            context=context,
            conversation=conversation,
        )

        agent_router = (
            conversation
            if self.coordinator_factory is None
            else self.coordinator_factory(conversation, context.outbound_queue)
        )

        if agent_router is not conversation:
            # Group Coordinator: the router starts and closes the Conversation
            # itself (plus its check-in timer), so it takes its worker slot.
            workers = [agent_router if w is conversation else w for w in workers]

        return SessionComponents(
            agent_router=agent_router,
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
            usage_tracker=self.usage_tracker,
            listening_mode=self.listening_mode,
        )

    async def create_workers(
        self,
        *,
        context: SessionContext,
        conversation: Conversation,
    ) -> list[SessionWorker]:
        return [conversation]
