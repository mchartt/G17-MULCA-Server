import base64
import struct

from muca_server.audio import AudioChunk


def encode_client_audio(samples: list[float]) -> str:
    """Encode audio the way the client does: raw float32 LE, then base64."""

    raw = struct.pack(f"<{len(samples)}f", *samples)
    return base64.b64encode(raw).decode("ascii")


def test_round_trip_preserves_the_wire_payload():
    encoded = encode_client_audio([0.0, 0.5, -0.5, 1.0])

    chunk = AudioChunk.from_base64(encoded, speaker=3)

    assert chunk.to_base64() == encoded
    assert chunk.speaker == 3


def test_sample_count_counts_four_byte_samples():
    chunk = AudioChunk.from_base64(
        encode_client_audio([0.1] * 480),
    )

    assert chunk.sample_count == 480
    assert chunk.duration_ms == 20


def test_empty_chunk_is_falsy():
    assert not AudioChunk(data=b"")
    assert AudioChunk(data=b"\x00\x00\x00\x00")
