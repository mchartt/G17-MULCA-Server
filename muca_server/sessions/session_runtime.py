from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from ..events import AgentRouterProtocol, DispatchResult, dispatch
from ..protocol import (
    CONNECT_EVENTS,
    EndSession,
    ErrorMessage,
    parse_incoming_message,
    OutgoingProtocolMessage,
    SetCostAlertMessage,
    SetModelMessage,
    serialize_outgoing_message,
)
from .session_components import SessionWorker
from .session_state import SessionState

logger = logging.getLogger(__name__)

CLIENT_AUTHENTICATION_TIMEOUT = 5

class SessionRuntime:
    # Sessions with agents built, across the whole server. Each one holds
    # two OpenAI Realtime connections (main and intervention agent).
    active_sessions = 0
    max_sessions = int(os.environ.get("MUCA_MAX_SESSIONS", "50"))

    def __init__(
        self,
        *,
        session_id: UUID,
        websocket: WebSocket,
        state: SessionState,
        agent_router: AgentRouterProtocol,
        outbound_queue: asyncio.Queue[
            OutgoingProtocolMessage | EndSession
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
        self._counted_since: float | None = None

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

            if isinstance(message, SetModelMessage):
                # 1012: every live session (model changed) reopens on it.
                await self.run_admin(message, close_code=1012, close_reason="Model changed")
                return

            if isinstance(message, SetCostAlertMessage):
                # 1000: nothing a connected agent is doing depends on this,
                # so no session needs to reopen -- just confirm and close.
                await self.run_admin(message, close_code=1000, close_reason="Cost alert settings changed")
                return

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
                if SessionRuntime.active_sessions >= SessionRuntime.max_sessions:
                    logger.warning(
                        "Session %s refused for %s: %d/%d sessions active",
                        self.session_id,
                        getattr(message, "conversation_id", None),
                        SessionRuntime.active_sessions,
                        SessionRuntime.max_sessions,
                    )
                    await self.websocket.close(code=1013, reason="Server busy")
                    return

                SessionRuntime.active_sessions += 1
                self._counted_since = time.monotonic()
                logger.info(
                    "Session %s opened for %s (dm=%s), active=%d/%d",
                    self.session_id,
                    getattr(message, "conversation_id", None),
                    getattr(message, "is_dm", False),
                    SessionRuntime.active_sessions,
                    SessionRuntime.max_sessions,
                )

                # Apply the handshake's identity (conversation_id, is_dm)
                # before the session's components start, so working memory
                # is created under the right scope/privacy from the first
                # turn onward -- not after the fact.
                try:
                    await dispatch(message, self.agent_router)
                except Exception:
                    # Typically the provider refusing the main agent's
                    # connection: nothing in this session can work then.
                    logger.exception(
                        "Session %s could not connect its main agent",
                        self.session_id,
                    )
                    await self.websocket.close(
                        code=1011,
                        reason="Cannot connect the main agent",
                    )
                    return

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
        except RuntimeError:
            # Receiving on a socket send_loop has already closed.
            logger.info(
                "Session %s ended after its socket closed",
                self.session_id,
            )
        finally:
            await self.close()

    async def run_admin(
        self,
        message: SetModelMessage | SetCostAlertMessage,
        *,
        close_code: int,
        close_reason: str,
    ) -> None:
        """
        An admin request sent instead of `connect`: no agents are built, so
        it still works when the current model can't connect at all.
        """

        try:
            await dispatch(message, self.agent_router)
        except Exception as exc:
            logger.warning("Session %s: %s refused: %s", self.session_id, message.event, exc)
            await self.websocket.send_text(
                serialize_outgoing_message(
                    ErrorMessage(message=f"{message.event} failed: {exc}"),
                )
            )
            await self.websocket.close(code=1008, reason="Refused")
            return

        await self.websocket.close(code=close_code, reason=close_reason)

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

            try:
                result = await dispatch(message, self.agent_router)
            except Exception as exc:
                # One failed request (a provider call that errored, an
                # auxiliary agent that could not connect) must not take the
                # whole conversation down with it.
                logger.exception(
                    "Session %s failed to handle %s",
                    self.session_id,
                    message.event,
                )
                await self.outbound_queue.put(
                    ErrorMessage(
                        message=f"{message.event} failed: {exc}",
                        agent_guid=getattr(
                            message,
                            "target_agent_guid",
                            None,
                        ) or None,
                    )
                )
                continue

            if result == DispatchResult.CLOSE_SESSION:
                break

    async def send_loop(self) -> None:
        while not self._closed:
            message = await self.outbound_queue.get()

            if isinstance(message, EndSession):
                # A worker can't end its own session (it would be cancelling
                # itself), so it asks here. Closing the socket ends
                # receive_loop through the same path as a client that left,
                # and run()'s finally closes everything else.
                logger.info(
                    "Ending session %s: %s",
                    self.session_id,
                    message.reason,
                )
                self.outbound_queue.task_done()
                with suppress(Exception):
                    await self.websocket.close(
                        code=message.code,
                        reason=message.reason,
                    )
                return

            try:
                payload = serialize_outgoing_message(
                    message
                )

                await self.websocket.send_text(payload)
            except Exception as exc:
                # The socket is gone. Close it for good so receive_loop ends
                # too, instead of the session's workers blocking forever on
                # a full outbound queue nobody drains.
                if isinstance(exc, WebSocketDisconnect):
                    # The client left while replies were still in flight.
                    logger.info(
                        "Session %s: client gone, dropping unsent messages",
                        self.session_id,
                    )
                else:
                    logger.warning(
                        "Session %s could not send, closing",
                        self.session_id,
                        exc_info=True,
                    )
                with suppress(Exception):
                    await self.websocket.close()
                return
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

        if self._counted_since is not None:
            SessionRuntime.active_sessions -= 1
            logger.info(
                "Session %s closed after %.0fs, active=%d/%d",
                self.session_id,
                time.monotonic() - self._counted_since,
                SessionRuntime.active_sessions,
                SessionRuntime.max_sessions,
            )

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

        # RuntimeError: already closed (send_loop or an EndSession did it).
        # WebSocketDisconnect: the client is already gone without a close
        # handshake (e.g. this server shutting down under it).
        with suppress(RuntimeError, WebSocketDisconnect):
            await self.websocket.close()
