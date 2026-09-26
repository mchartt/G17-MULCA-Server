"""
Sample-format conversions between the MUCA wire format and providers.

The wire carries raw IEEE float32 little-endian mono samples (see
AudioChunk); realtime providers want 16-bit PCM. The conversions mirror the
Unity package's PcmUtility.cs exactly (clamp, scale by 32767, truncate), so
a Unity client and this server agree sample for sample.
"""

from __future__ import annotations

import numpy as np

PCM16_SCALE = 32767.0


def float32_to_pcm16(data: bytes) -> bytes:
    """float32 LE samples in [-1, 1] -> int16 LE samples (PcmUtility.SamplesToPcm)."""

    samples = np.frombuffer(data, dtype="<f4")
    scaled = np.clip(samples, -1.0, 1.0) * PCM16_SCALE

    # astype truncates toward zero, like C#'s (short) cast.
    return scaled.astype("<i2").tobytes()


def pcm16_to_float32(data: bytes) -> bytes:
    """int16 LE samples -> float32 LE samples (PcmUtility.PcmToFloats)."""

    usable = len(data) - (len(data) % 2)
    samples = np.frombuffer(data[:usable], dtype="<i2")

    return (samples.astype(np.float32) / PCM16_SCALE).astype("<f4").tobytes()


def resample(
    samples: np.ndarray,
    source_rate: int,
    target_rate: int,
) -> np.ndarray:
    """Linear-interpolation resampling of mono samples (PcmUtility.Resample)."""

    if source_rate == target_rate or samples.size == 0:
        return samples

    target_length = max(
        1,
        int(round(samples.size * target_rate / source_rate)),
    )
    positions = np.arange(target_length) * (source_rate / target_rate)

    return np.interp(
        positions,
        np.arange(samples.size),
        samples,
    ).astype(samples.dtype)


def resample_float32(
    data: bytes,
    source_rate: int,
    target_rate: int,
) -> bytes:
    samples = np.frombuffer(data, dtype="<f4")

    return resample(samples, source_rate, target_rate).astype("<f4").tobytes()


def split_float32(data: bytes, max_samples: int) -> list[bytes]:
    """Split float32 audio into frames of at most max_samples samples."""

    step = max_samples * 4

    return [
        data[offset:offset + step]
        for offset in range(0, len(data), step)
    ]
