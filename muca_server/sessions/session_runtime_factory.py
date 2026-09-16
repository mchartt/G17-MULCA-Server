import asyncio
from uuid import uuid4

from fastapi import WebSocket

from .session_runtime import SessionRuntime
from .session_state import SessionState
from .session_components import SessionComponentsFactory, SessionContext
from .session_components_factory import DefaultSessionComponentsFactory

class SessionRuntimeFactory:

    def __init__(
        self,
        components_factory: SessionComponentsFactory | None = None,
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
            agent_router=components.agent_router,
            outbound_queue=outbound_queue,
            workers=components.workers,
        )
