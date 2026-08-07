from __future__ import annotations

import logging
from typing import Protocol
from enum import StrEnum

from ..protocol import (
    AudioStreamMessage,
    ConnectMainAssistantMessage,
    CreateAuxiliaryAgentMessage,
    DisconnectMessage,
    IncomingProtocolMessage,
    InterruptMessage,
    PlaybackFrameMessage,
    RemoveAuxiliaryAgentMessage,
    TextMessage,
    TriggerResponseMessage,
)

logger = logging.getLogger(__name__)


class AudioRouterProtocol(Protocol):
    """
    Interface which defines methods to forward audio to audio specialized components
    """
    
    async def publish_user_audio(
        self,
        message: AudioStreamMessage,
    ) -> None:
        """Forwards user audio to main and (if present) auxiliary agent"""
        ...

    async def publish_playback(
        self,
        message: PlaybackFrameMessage,
    ) -> None:
        """Forwards the already played audio to main and (if present) auxiliary agent"""
        ...

class DispatchResult(StrEnum):
    CONTINUE = "continue"
    CLOSE_SESSION = "close_session"

class EventDispatcher:
    """
    Forward the validated protocol messages to the application components.

    The dispatcher does not:
    - parse JSON;
    - decode audio directly;
    - write to the WebSocket;
    - modify the server's state directly.

    Every message received has already been validated by IncomingProtocolMessage.
    """
    def __init__(
        self,
        *,
        audio_router
    ) -> None:
        self.audio_router = audio_router
        self._started = False
        self._closed = False

    async def start(self) -> None:
        """
        Activates dispatcher.
        """
        if self._closed:
            raise RuntimeError(
                "Cannot start a closed EventDispatcher"
            )

        self._started = True

    async def dispatch(
        self,
        message: IncomingProtocolMessage,
    ) -> DispatchResult:
        """
        Routes messages to the suitable components.

        Raise RuntimeError if Dispatcher is closed or not active yet.
        """
        if not self._started:
            raise RuntimeError(
                "EventDispatcher has not been started"
            )

        if self._closed:
            raise RuntimeError(
                "EventDispatcher is already closed"
            )
        
        match message:
            case AudioStreamMessage():
                await self._handle_audio_stream(message)

            case PlaybackFrameMessage():
                await self._handle_playback_frame(message)

            case TextMessage():
                await self._handle_text_message(message)

            case TriggerResponseMessage():
                await self._handle_trigger_response(message)

            case InterruptMessage():
                await self._handle_interrupt(message)

            case ConnectMainAssistantMessage():
                await self._handle_connect_main_assistant()

            case CreateAuxiliaryAgentMessage():
                await self._handle_create_auxiliary_agent(
                    message
                )

            case RemoveAuxiliaryAgentMessage():
                await self._handle_remove_auxiliary_agent(
                    message
                )

            case DisconnectMessage():
                await self._handle_disconnect()
                return DispatchResult.CLOSE_SESSION

            case _:
                raise TypeError(
                    "Unsupported incoming message type: "
                    f"{type(message).__name__}"
                )
        
        return DispatchResult.CONTINUE

    async def close(self) -> None:
        """
        Stops the application resources managed by the dispatcher.
        """
        if self._closed:
            return

        self._closed = True
        self._started = False


    async def _handle_audio_stream(
        self,
        message: AudioStreamMessage,
    ) -> None:
        pass

    async def _handle_playback_frame(
        self,
        message: PlaybackFrameMessage,
    ) -> None:
        pass

    async def _handle_text_message(
        self,
        message: TextMessage,
    ) -> None:
        pass

    async def _handle_trigger_response(
        self,
        message: TriggerResponseMessage,
    ) -> None:
        pass

    async def _handle_interrupt(
        self,
        message: InterruptMessage,
    ) -> None:
        pass

    async def _handle_connect_main_assistant(
        self,
    ) -> None:
        pass

    async def _handle_create_auxiliary_agent(
        self,
        message: CreateAuxiliaryAgentMessage,
    ) -> None:
        pass

    async def _handle_remove_auxiliary_agent(
        self,
        message: RemoveAuxiliaryAgentMessage,
    ) -> None:
        pass

    async def _handle_disconnect(self) -> None:
        pass
