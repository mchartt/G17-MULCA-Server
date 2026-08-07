from __future__ import annotations

import json
import asyncio
from contextlib import suppress
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from events import EventDispatcher, DispatchResult
from protocol import (
    parse_incoming_message,
    OutgoingProtocolMessage,
    serialize_outgoing_message,
)
from sessions import SessionState

CLIENT_AUTHENTICATION_TIMEOUT = 5

class SessionRuntime:
    def __init__(
        self,
        *,
        session_id: UUID,
        websocket: WebSocket,
        state: SessionState,
        dispatcher: EventDispatcher,
        outbound_queue: asyncio.Queue[
            OutgoingProtocolMessage
        ],
        workers: list, #List of coroutines, each one exposes a start and close method
    ) -> None:
        self.session_id = session_id
        self.websocket = websocket
        self.state = state
        self.dispatcher = dispatcher
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
        
            message = json.loads(raw_message)
            
            print(message)
        
            if (message.get("event") != "connect_main_assistant"): #TODO: nel client va sostituito con connect_main_agent
                print("Identification failed: Closing websocket")
                await self.websocket.close(
                    code = 1008,
                    reason="Bad request"
                )
                return
            else:
                await self.start()
                await self.receive_loop()
            
        except asyncio.TimeoutError:
            await self.websocket.close(
                code=1008,
                reason="Cannot establish connection"
            )
        except WebSocketDisconnect:
            print("Client disconnected")
            self.state.is_connected = False
        finally:
            await self.close()

    async def start(self) -> None:
        self.state.is_connected = True

        for worker in self.workers:
            await worker.start()

        await self.dispatcher.start()

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
                print(message)
            except Exception as exc:
                await self.handle_protocol_error(exc)
                continue

            result = await self.dispatcher.dispatch(message)
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
        print(
            f"Protocol error in session "
            f"{self.session_id}: {type(error).__name__}\n"
            f"{error}"
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