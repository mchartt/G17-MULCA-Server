# app/sessions/factory.py

import asyncio
from uuid import uuid4

from fastapi import WebSocket

from ..audio import AudioRouter
from ..events import EventDispatcher
from .session_runtime import SessionRuntime
from .session_state import SessionState


class SessionRuntimeFactory:
    
    async def create(
        self,
        websocket: WebSocket,
    ) -> SessionRuntime:
        session_id = uuid4()

        state = SessionState(
            session_id=session_id,
        )

        audio_router = AudioRouter()

        outbound_queue = asyncio.Queue(maxsize=128)

        dispatcher = EventDispatcher(
            audio_router=audio_router
        )

        return SessionRuntime(
            session_id=session_id,
            websocket=websocket,
            state=state,
            dispatcher=dispatcher,
            outbound_queue=outbound_queue,
            workers=[], #TODO: definire worker dei vari componenti (es. Main agent)
        )
