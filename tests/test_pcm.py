"""
The sample conversions must agree with the Unity client's PcmUtility.cs
sample for sample: clamp to [-1, 1], scale by 32767, truncate toward zero.
"""

import struct

from muca_server.audio.pcm import (
    float32_to_pcm16,
    pcm16_to_float32,
    resample_float32,
    split_float32,
)


def floats(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_floats(data: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(data) // 4}f", data)


def test_float32_to_pcm16_scales_clamps_and_truncates_like_unity():
    pcm = float32_to_pcm16(floats(0.0, 1.0, -1.0, 2.0, -2.0, 0.5, -0.5))

    # (short)(0.5 * 32767) == 16383 in C#: truncation, not rounding.
    assert struct.unpack("<7h", pcm) == (
        0,
        32767,
        -32767,
        32767,
        -32767,
        16383,
        -16383,
    )


def test_pcm16_to_float32_divides_by_32767_like_unity():
    samples = unpack_floats(
        pcm16_to_float32(struct.pack("<3h", 0, 32767, -32767)),
    )

    assert samples == (0.0, 1.0, -1.0)


def test_a_round_trip_stays_within_one_pcm16_step():
    original = (0.1, -0.25, 0.9999, -0.3333)
    restored = unpack_floats(
        pcm16_to_float32(float32_to_pcm16(floats(*original))),
    )

    for before, after in zip(original, restored):
        assert abs(before - after) <= 1 / 32767


def test_an_odd_trailing_byte_is_ignored():
    assert pcm16_to_float32(b"\x00\x00\x01") == floats(0.0)


def test_resampling_48k_to_24k_halves_the_sample_count():
    data = floats(*([0.5] * 960))

    resampled = resample_float32(data, 48_000, 24_000)

    assert len(resampled) // 4 == 480
    assert set(unpack_floats(resampled)) == {0.5}


def test_resampling_to_the_same_rate_is_a_no_op():
    data = floats(0.1, 0.2)

    assert resample_float32(data, 24_000, 24_000) == data


def test_split_keeps_every_sample_in_bounded_frames():
    data = floats(*range(7))

    frames = split_float32(data, 3)

    assert [len(frame) // 4 for frame in frames] == [3, 3, 1]
    assert b"".join(frames) == data
