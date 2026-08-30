import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from fastapi import WebSocket

from ..events import EventDispatcher
from ..protocol import OutgoingProtocolMessage
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
    outbound_queue: asyncio.Queue[OutgoingProtocolMessage]


@dataclass(slots=True)
class SessionComponents:
    dispatcher: EventDispatcher
    workers: list[SessionWorker] = field(
        default_factory=list,
    )


class SessionComponentsFactory(Protocol):

    async def create(
        self,
        context: SessionContext,
    ) -> SessionComponents:
        ...