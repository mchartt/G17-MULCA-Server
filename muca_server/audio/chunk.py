import base64
from dataclasses import dataclass

DEFAULT_SAMPLE_RATE = 24_000
NO_SPEAKER = -1


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """
    A mono audio frame as it travels on the MUCA WebSocket.

    `data` holds raw IEEE float32 little-endian samples, which is the
    client's wire format before base64. Providers that want PCM16
    convert on their own leg.
    """

    data: bytes
    sample_rate: int = DEFAULT_SAMPLE_RATE
    speaker: int = NO_SPEAKER

    @classmethod
    def from_base64(
        cls,
        encoded: str,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        speaker: int = NO_SPEAKER,
    ) -> "AudioChunk":
        return cls(
            data=base64.b64decode(encoded, validate=True),
            sample_rate=sample_rate,
            speaker=speaker,
        )

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def sample_count(self) -> int:
        return len(self.data) // 4

    @property
    def duration_ms(self) -> int:
        return (self.sample_count * 1000) // self.sample_rate

    def __bool__(self) -> bool:
        return bool(self.data)
