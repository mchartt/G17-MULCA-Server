"""
Live smoke test against a running MUCA-Server (python -m muca_server.run_server).

Acts as a minimal voice client, the way docs/PROTOCOL.md describes one:

1. connects and creates the intervention agent;
2. sends a text message that forces a reply ("@mainllm ...") and checks a
   text-modality assistant_text comes back;
3. with --wav, streams that recording as one user's speech in real time
   (100 ms frames, then heartbeat frames while "playing" the reply), writes
   the spoken reply to --out, and prints the state_update transitions.

Listen to the output file: clean speech means the float32/PCM16 conversions
are right both ways; noise means they are not.

Usage:
    python scripts/smoke_client.py [--url ws://localhost:8000/]
        [--wav question.wav] [--out reply.wav]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import wave

import numpy as np
import websockets

RATE = 24_000
FRAME_MS = 100
FRAME_SAMPLES = RATE * FRAME_MS // 1000


def load_wav(path: str) -> np.ndarray:
    """Any PCM16 WAV -> float32 mono at 24 kHz."""

    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: only 16-bit PCM WAV files are supported")

        rate = wav.getframerate()
        channels = wav.getnchannels()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")

    mono = samples.reshape(-1, channels).mean(axis=1) / 32767.0
    if rate != RATE:
        positions = np.arange(int(mono.size * RATE / rate)) * (rate / RATE)
        mono = np.interp(positions, np.arange(mono.size), mono)

    return mono.astype(np.float32)


def save_wav(path: str, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm.tobytes())


class Playback:
    """Pretends to play the reply in real time, like a real client would."""

    def __init__(self) -> None:
        self.received: list[np.ndarray] = []
        self.samples = 0
        self.started_at: float | None = None

    def add(self, encoded: str) -> None:
        chunk = np.frombuffer(base64.b64decode(encoded), dtype="<f4")
        self.received.append(chunk)
        self.samples += chunk.size
        if self.started_at is None:
            self.started_at = time.monotonic()

    def clear(self) -> None:
        self.samples = 0
        self.started_at = None

    def played_ms(self) -> int:
        if self.started_at is None:
            return 0
        elapsed = int((time.monotonic() - self.started_at) * 1000)
        return min(elapsed, self.samples * 1000 // RATE)

    def is_notifying(self) -> bool:
        return (
            self.started_at is not None
            and self.played_ms() < self.samples * 1000 // RATE
        )


async def run(url: str, wav_path: str | None, out_path: str) -> None:
    async with websockets.connect(url, max_queue=None) as ws:
        await ws.send(json.dumps({"event": "connect"}))
        await ws.send(
            json.dumps(
                {
                    "event": "create_auxiliary_agent",
                    "auxiliaryAgentType": "intervention_detection",
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "event": "participants",
                    "participants": [{"id": 1, "name": "Tester"}],
                }
            )
        )

        # -- text -------------------------------------------------------
        await ws.send(
            json.dumps(
                {
                    "event": "user_text",
                    "speaker": 1,
                    "speakerName": "Tester",
                    "text": "@mainllm say hello in one short sentence.",
                }
            )
        )

        reply = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if message["event"] == "error":
                print("server error:", message["message"])
            if message["event"] != "assistant_text":
                continue

            reply += message["text"]
            if message["isFinal"]:
                assert message["modality"] == "text", message
                break
        else:
            raise SystemExit("FAIL: no text reply within 30 s")

        print(f"text reply OK: {reply!r}")

        if wav_path is None:
            return

        # -- voice ------------------------------------------------------
        speech = load_wav(wav_path)
        playback = Playback()
        transitions: list[str] = []
        answered = asyncio.Event()

        async def receive() -> None:
            last = None
            async for raw in ws:
                message = json.loads(raw)
                event = message["event"]

                if event == "state_update":
                    if message.get("clearAudioQueue"):
                        playback.clear()
                    if message.get("audio"):
                        playback.add(message["audio"])

                    state = (
                        f"speaker={message['currentSpeaker']} "
                        f"answering={message['isAnswering']}"
                    )
                    if state != last:
                        transitions.append(state)
                        last = state

                    if (
                        message["assistantSpokeLast"]
                        and not message["isAnswering"]
                        and playback.samples
                        and not message.get("audio")
                    ):
                        answered.set()

                elif event == "assistant_text" and message["isFinal"]:
                    print(f"spoken reply transcript done ({message['modality']})")

                elif event == "error":
                    print("server error:", message["message"])

        receiver = asyncio.create_task(receive())

        async def send_frame(samples: np.ndarray, speaker: int) -> None:
            await ws.send(
                json.dumps(
                    {
                        "event": "audio_stream",
                        "speaker": speaker,
                        "isNotifying": playback.is_notifying(),
                        "playbackTimeMs": playback.played_ms(),
                        "audio": base64.b64encode(
                            samples.astype("<f4").tobytes()
                        ).decode("ascii")
                        if samples.size
                        else "",
                    }
                )
            )

        print(f"streaming {speech.size / RATE:.1f} s of speech...")
        for offset in range(0, speech.size, FRAME_SAMPLES):
            await send_frame(speech[offset:offset + FRAME_SAMPLES], speaker=1)
            await asyncio.sleep(FRAME_MS / 1000)

        # Heartbeats while nobody talks and the reply "plays".
        deadline = time.monotonic() + 45
        while not answered.is_set() and time.monotonic() < deadline:
            await send_frame(np.zeros(0, dtype=np.float32), speaker=-1)
            await asyncio.sleep(FRAME_MS / 1000)

        receiver.cancel()

        print("state transitions:")
        for state in transitions:
            print("  ", state)

        if not playback.received:
            raise SystemExit(
                "FAIL: no spoken reply (the intervention agent may simply "
                "have judged that no reply was needed -- ask MULCA a direct "
                "question in the recording)"
            )

        save_wav(out_path, np.concatenate(playback.received))
        print(f"spoken reply written to {out_path}: listen to it")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--url", default="ws://localhost:8000/")
    parser.add_argument("--wav", help="a recording of a question to MULCA")
    parser.add_argument("--out", default="reply.wav")
    args = parser.parse_args()

    asyncio.run(run(args.url, args.wav, args.out))


if __name__ == "__main__":
    main()
