"""Преобразование float ↔ сырые PCM-байты (то, что гоняем через aplay/arecord)."""
from __future__ import annotations

import numpy as np

FORMATS = ("S32_LE", "S24_3LE", "S24_LE", "S16_LE")      # порядок предпочтения
BYTES = {"S32_LE": 4, "S24_3LE": 3, "S24_LE": 4, "S16_LE": 2}


def to_pcm(x, fmt):
    """float32/64 (n, ch) в [-1, 1] → байты. Перегрузка обрезается, не заворачивается."""
    x = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    if fmt == "S32_LE":
        return np.round(x * 2147483647.0).astype("<i4").tobytes()
    if fmt == "S24_LE":
        return np.round(x * 8388607.0).astype("<i4").tobytes()
    if fmt == "S24_3LE":
        v = np.round(x * 8388607.0).astype("<i4")
        return v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    if fmt == "S16_LE":
        return np.round(x * 32767.0).astype("<i2").tobytes()
    raise ValueError(fmt)


def from_pcm(buf, fmt, channels):
    """байты → float64 (n, channels), полная шкала = ±1.0."""
    if fmt == "S32_LE":
        v = np.frombuffer(buf, dtype="<i4").astype(np.float64) / 2147483648.0
    elif fmt == "S24_LE":
        v = np.frombuffer(buf, dtype="<i4").astype(np.int64)
        v = ((v & 0xFFFFFF) ^ 0x800000) - 0x800000
        v = v.astype(np.float64) / 8388608.0
    elif fmt == "S24_3LE":
        b = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3).astype(np.int64)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = ((v ^ 0x800000) - 0x800000).astype(np.float64) / 8388608.0
    elif fmt == "S16_LE":
        v = np.frombuffer(buf, dtype="<i2").astype(np.float64) / 32768.0
    else:
        raise ValueError(fmt)
    return v.reshape(-1, channels)
