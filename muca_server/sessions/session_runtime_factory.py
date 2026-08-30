# app/sessions/factory.py

import asyncio
from uuid import uuid4

from fastapi import WebSocket

from ..audio import AudioRouter
from ..events import EventDispatcher
from .session_runtime import SessionRuntime
from .session_state import SessionState
from .session_components import SessionContext
from .session_components_factory import DefaultSessionComponentsFactory

class SessionRuntimeFactory:
    
    def __init__(
        self,
        components_factory=None
    ) -> None:
        self.components_factory = (
            components_factory
            or DefaultSessionComponentsFactory()
        )
    
    async def create(
        self,
        websocket: WebSocket,
    ) -> SessionRuntime:
        session_id = uuid4()

        state = SessionState(
            session_id=session_id,
        )

        outbound_queue = asyncio.Queue(maxsize=128)

        context = SessionContext(
            session_id=session_id,
            websocket=websocket,
            state=state,
            outbound_queue=outbound_queue,
        )
        
        components = await self.components_factory.create(
            context=context
        )

        return SessionRuntime(
            session_id=session_id,
            websocket=websocket,
            state=state,
            dispatcher=components.dispatcher,
            outbound_queue=outbound_queue,
            workers=components.workers,
        )
