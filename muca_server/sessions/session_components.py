import asyncio
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from fastapi import WebSocket

from ..events import AgentRouterProtocol
from ..protocol import EndSession, OutgoingProtocolMessage
from .session_state import SessionState


class SessionWorker(Protocol):

    async def start(self) -> None:
        ...

    async def close(self) -> None:
        ...


@dataclass(slots=True)
class SessionContext:
    session_id: UUID
    websocket: WebSocket
    state: SessionState
    outbound_queue: asyncio.Queue[OutgoingProtocolMessage | EndSession]


@dataclass(slots=True)
class SessionComponents:
    agent_router: AgentRouterProtocol
    workers: list[SessionWorker] = field(
        default_factory=list,
    )


class SessionComponentsFactory(Protocol):

    async def create(
        self,
        context: SessionContext,
    ) -> SessionComponents:
        ...