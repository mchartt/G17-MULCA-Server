import asyncio
import base64
import json
import time

from fastapi import WebSocket # type: ignore
from dataclasses import dataclass

AUDIO_SAMPLE_RATE = 24000
AUDIO_CHANNELS = 1
QUEUE_MAX_SIZE = 128

@dataclass(frozen=True, slots=True)
class AudioFrame:
    pcm: bytes
    speaker: int
    received_at: float
    is_notifying: bool
    playback_time_ms: int
    source: str

@dataclass(frozen=True, slots=True)
class TextMessage:
    role: str
    text: str
    target_agent: str

@dataclass(frozen=True, slots=True)
class ManageAgentsMessage:
    agentType: str

class SessionRuntime:
    def __init__(self, ws: WebSocket):
        self.websocket = ws

        self.main_agent_queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE) # audio queue for main agent
        self.auxiliary_agent_queue: asyncio.Queue[AudioFrame] = None # audio queue for auxiliary agent
        self.outbound_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE) # queue for outgoing messages

        self.tasks: list[asyncio.Task] = []

    async def start(self):
        self.tasks = [
            asyncio.create_task(self.main_agent_worker()), 
            asyncio.create_task(self.outbound_worker()),
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def handle_message(self, message: dict):
        event = message.get("event")

        # TODO: ordina gli eventi in ordine di "più probabile"
        if event == "audio_stream":
            await self.handle_user_audio(message)

        elif event == "playback_frame":
            await self.handle_playback_audio(message)

        elif event == "text_message":
            pass

        elif event == "trigger_response":
            pass

        elif event == "interrupt_response":
            pass

        elif event == "create_auxiliary_agent":
            pass

        elif event == "remove_auxiliary_agent":
            pass

        elif event == "connect_main_assistant":
            pass

        elif event == "disconnect":
            pass

        else:
            pass

    async def handle_user_audio(self, message: dict):
        pcm = base64.b64decode(message["audio"], validate=True)

        frame = AudioFrame(
            pcm=pcm,
            speaker=message.get("speaker", -1),
            received_at=time.monotonic(),
            is_notifying=message.get("isNotifying", False),
            playback_time_ms=message.get("playbackTimeMs", 0),
            source="user",
        )

        await self.main_agent_queue.put(frame)

    async def handle_playback_audio(self, message: dict):
        pcm = base64.b64decode(message["audio"], validate=True)

        frame = AudioFrame(
            pcm=pcm,
            speaker=message.get("speaker", -1),
            received_at=time.monotonic(),
            is_notifying=message.get("isNotifying", False),
            playback_time_ms=message.get("playbackTimeMs", 0),
            source="assistant_playback",
        )

        if self.auxiliary_agent_queue is not None:
            self.auxiliary_agent_queue.put(frame)
        
    async def main_agent_worker(self):
        pass

    async def auxiliary_agent_worker(self):
        pass

    async def websocket_writer(self):
        while True:
            message = await self.outbound_queue.get()

            try:
                await self.websocket.send_json(message)
            finally:
                self.outbound_queue.task_done()