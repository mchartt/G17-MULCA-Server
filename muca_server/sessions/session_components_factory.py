from collections.abc import Callable

from .session_components import SessionComponents, SessionContext, SessionWorker
from ..agents import AgentFactory, Conversation, MainAgentOptions
from ..cognition import CognitionPolicy

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
    ) -> None:
        self.agent_factory = agent_factory
        self.policy_factory = policy_factory
        self.main_agent_options = (
            main_agent_options or MainAgentOptions()
        )

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
        )

    async def create_workers(
        self,
        *,
        context: SessionContext,
        conversation: Conversation,
    ) -> list[SessionWorker]:
        return [conversation]
