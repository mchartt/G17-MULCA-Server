from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from ..events import AgentRouterProtocol, DispatchResult, dispatch
from ..protocol import (
    CONNECT_EVENTS,
    parse_incoming_message,
    OutgoingProtocolMessage,
    serialize_outgoing_message,
)
from .session_components import SessionWorker
from .session_state import SessionState

logger = logging.getLogger(__name__)

CLIENT_AUTHENTICATION_TIMEOUT = 5

class SessionRuntime:
    def __init__(
        self,
        *,
        session_id: UUID,
        websocket: WebSocket,
        state: SessionState,
        agent_router: AgentRouterProtocol,
        outbound_queue: asyncio.Queue[
            OutgoingProtocolMessage
        ],
        workers: list[SessionWorker],
    ) -> None:
        self.session_id = session_id
        self.websocket = websocket
        self.state = state
        self.agent_router = agent_router
        self.outbound_queue = outbound_queue
        self.workers = workers

        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    async def run(self) -> None:
        try:
            await self.websocket.accept()
        
            # Client should identify in 5 seconds
            raw_message = await asyncio.wait_for(
                self.websocket.receive_text(),
                timeout=CLIENT_AUTHENTICATION_TIMEOUT,
            )
        
            try:
                message = parse_incoming_message(raw_message)
            except Exception:
                message = None

            if message is None or message.event not in CONNECT_EVENTS:
                logger.warning(
                    "Session %s failed identification, closing websocket",
                    self.session_id,
                )
                await self.websocket.close(
                    code = 1008,
                    reason="Bad request"
                )
                return
            else:
                # Apply the handshake's identity (conversation_id, is_dm)
                # before the session's components start, so working memory
                # is created under the right scope/privacy from the first
                # turn onward -- not after the fact.
                await dispatch(message, self.agent_router)
                await self.start()
                await self.receive_loop()

        except asyncio.TimeoutError:
            await self.websocket.close(
                code=1008,
                reason="Cannot establish connection"
            )
        except WebSocketDisconnect:
            logger.info(
                "Client disconnected from session %s",
                self.session_id,
            )
            self.state.is_connected = False
        finally:
            await self.close()

    async def start(self) -> None:
        self.state.is_connected = True

        for worker in self.workers:
            await worker.start()

        self._tasks.append(
            asyncio.create_task(
                self.send_loop(),
                name=f"session-{self.session_id}-send-loop",
            )
        )

    async def receive_loop(self) -> None:
        while not self._closed:
            raw_message = await self.websocket.receive_text()

            try:
                message = parse_incoming_message(raw_message)
            except Exception as exc:
                await self.handle_protocol_error(exc)
                continue

            result = await dispatch(message, self.agent_router)
            if result == DispatchResult.CLOSE_SESSION:
                break

    async def send_loop(self) -> None:
        while not self._closed:
            message = await self.outbound_queue.get()

            try:
                payload = serialize_outgoing_message(
                    message
                )

                await self.websocket.send_text(payload)
            finally:
                self.outbound_queue.task_done()

    async def handle_protocol_error(
        self,
        error: Exception,
    ) -> None:
        logger.warning(
            "Protocol error in session %s: %s\n%s",
            self.session_id,
            type(error).__name__,
            error,
        )

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        self.state.is_connected = False

        for worker in reversed(self.workers):
            with suppress(Exception):
                await worker.close()

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(
            *self._tasks,
            return_exceptions=True,
        )

        self._tasks.clear()

        with suppress(RuntimeError):
            await self.websocket.close()
