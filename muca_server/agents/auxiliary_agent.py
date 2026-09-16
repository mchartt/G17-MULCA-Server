from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from ..cognition.ports import InterventionSignal
from .options import AuxiliaryAgentOptions
from .realtime_agent import RealtimeAgent

logger = logging.getLogger(__name__)

DecisionHandler = Callable[
    [UUID, InterventionSignal | None],
    Awaitable[None],
]


class AuxiliaryAgent(RealtimeAgent):
    """
    An agent that observes the conversation without speaking in it.

    It never streams audio back to the users. What it produces reaches the
    client as raw provider messages, and its effect on the conversation comes
    from asking the main agent to act.
    """

    def __init__(
        self,
        *,
        options: AuxiliaryAgentOptions,
    ) -> None:
        super().__init__()

        self.options = options
        self.guid: UUID = options.agent_guid or uuid4()
        self.listen_to_assistant = options.listen_to_assistant

        # None means every user.
        self.listened_users: list[int] | None = options.listened_users

        # Fires once per judgement; None means "say nothing".
        self.on_decision: DecisionHandler | None = None

    async def _signal_decision(
        self,
        signal: InterventionSignal | None,
    ) -> None:
        if self.on_decision is not None:
            await self.on_decision(self.guid, signal)

    def listens_to_user(self, user_id: int) -> bool:
        if self.listened_users is None:
            return True

        return user_id in self.listened_users
