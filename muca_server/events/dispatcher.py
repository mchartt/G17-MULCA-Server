from __future__ import annotations

import logging
from typing import Protocol
from enum import StrEnum

from ..protocol import (
    AudioStreamMessage,
    ConnectAgentMessage,
    CreateAuxiliaryAgentMessage,
    DisconnectMessage,
    IncomingProtocolMessage,
    InterruptMessage,
    ParticipantsMessage,
    PlaybackFrameMessage,
    RawProviderMessage,
    RemoveAuxiliaryAgentMessage,
    TextMessage,
    TriggerResponseMessage,
    UserTextMessage,
)

logger = logging.getLogger(__name__)


class AgentRouterProtocol(Protocol):
    """
    Interface which defines how validated messages reach a session's agents.

    Every client message ends up at one or more agents, so this is the only
    component the dispatcher talks to. A target guid of None or "" addresses
    the main agent.
    """

    async def route_user_audio(
        self,
        message: AudioStreamMessage,
    ) -> None:
        """Forward user audio to the main agent and its subscribers."""
        ...

    async def route_playback_frame(
        self,
        message: PlaybackFrameMessage,
    ) -> None:
        """Forward already played agent audio to its subscribers."""
        ...

    async def route_text(self, message: TextMessage) -> None:
        ...

    async def route_user_text(
        self,
        message: UserTextMessage,
    ) -> None:
        """Forward one user's text utterance, and take the turn from it."""
        ...

    async def route_participants(
        self,
        message: ParticipantsMessage,
    ) -> None:
        """Record who is present, including those who have not spoken."""
        ...

    async def connect_agent(
        self,
        message: ConnectAgentMessage,
    ) -> None:
        ...

    async def disconnect_agent(
        self,
        message: DisconnectMessage,
    ) -> None:
        """Disconnect one agent, or every agent when no target is given."""
        ...

    async def create_auxiliary_agent(
        self,
        message: CreateAuxiliaryAgentMessage,
    ) -> None:
        """Create an auxiliary agent and announce its guid to the client."""
        ...

    async def remove_auxiliary_agent(
        self,
        message: RemoveAuxiliaryAgentMessage,
    ) -> None:
        ...

    async def trigger_response(
        self,
        message: TriggerResponseMessage,
    ) -> None:
        ...

    async def interrupt_response(
        self,
        message: InterruptMessage,
    ) -> None:
        ...


class DispatchResult(StrEnum):
    CONTINUE = "continue"
    CLOSE_SESSION = "close_session"


async def dispatch(
    message: IncomingProtocolMessage | RawProviderMessage,
    agent_router: AgentRouterProtocol,
) -> DispatchResult:
    """
    Forward one validated message to the agents, and say whether the
    session should carry on.

    Dispatching does not parse JSON, decode audio, write to the WebSocket
    or touch the session state.
    """

    match message:
        case AudioStreamMessage():
            await agent_router.route_user_audio(message)

        case PlaybackFrameMessage():
            await agent_router.route_playback_frame(message)

        case TextMessage():
            await agent_router.route_text(message)

        case UserTextMessage():
            await agent_router.route_user_text(message)

        case ParticipantsMessage():
            await agent_router.route_participants(message)

        case TriggerResponseMessage():
            await agent_router.trigger_response(message)

        case InterruptMessage():
            await agent_router.interrupt_response(message)

        case ConnectAgentMessage():
            await agent_router.connect_agent(message)

        case CreateAuxiliaryAgentMessage():
            await agent_router.create_auxiliary_agent(message)

        case RemoveAuxiliaryAgentMessage():
            await agent_router.remove_auxiliary_agent(message)

        case DisconnectMessage():
            await agent_router.disconnect_agent(message)

            # Disconnecting one agent leaves the session running.
            if message.target_agent_guid:
                return DispatchResult.CONTINUE

            return DispatchResult.CLOSE_SESSION

        case RawProviderMessage():
            # Provider passthrough (Unity's SendAnythingAsync). Not forwarded:
            # the server owns each provider session's configuration, and a
            # client's session.update (often in an older API dialect) would
            # silently undo it. Forward it from here if a client ever needs to.
            logger.info(
                "Ignoring provider passthrough frame of type %r",
                message.payload.get("type"),
            )

        case _:
            raise TypeError(
                "Unsupported incoming message type: "
                f"{type(message).__name__}"
            )

    return DispatchResult.CONTINUE
