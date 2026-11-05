"""Преобразование float ↔ сырые PCM-байты (то, что гоняем через aplay/arecord).

0.1.12-P0: декодирование блочное — временные массивы ограничены размером блока,
а не длиной записи. Потоковые функции читают capture и пишут float32 WAV одним
проходом, так что пик памяти steady не зависит от длительности захвата.
"""
from __future__ import annotations

import os

import numpy as np

FORMATS = ("S32_LE", "S24_3LE", "S24_LE", "S16_LE")      # порядок предпочтения
BYTES = {"S32_LE": 4, "S24_3LE": 3, "S24_LE": 4, "S16_LE": 2}
CHUNK_BYTES = 1 << 20        # 1 МиБ — блок потокового декодирования
FLOAT32_WAV_HEADER = 44      # RIFF+fmt(16, IEEE float)+data, без fact


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


def _decode_bytes_chunk(buf, fmt, channels):
    """Декодирование одного блока байтов → float64 (n, channels).

    Все временные массивы ограничены размером блока.
    """
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


def _chunk_bytes(fmt, channels):
    """Размер блока в байтах, кратный размеру кадра."""
    frame = BYTES[fmt] * channels
    return max(frame, (CHUNK_BYTES // frame) * frame)


def from_pcm(buf, fmt, channels):
    """байты → float64 (n, channels), полная шкала = ±1.0.

    Декодирование блочное: полноразмерные int64-массивы (N, 3) и цепочки
    промежуточных массивов не создаются; итоговый float64 по контракту
    возвращается целиком (для длинных capture используйте StreamPcmReader
    или stream_to_float32_wav — они держат в памяти только блок).
    """
    channels = int(channels)
    frame = BYTES[fmt] * channels
    if len(buf) % frame:
        raise ValueError(f"длина буфера {len(buf)} не кратна размеру кадра {frame}")
    step = _chunk_bytes(fmt, channels)
    if not len(buf):
        return np.empty((0, channels), dtype=np.float64)
    parts = [_decode_bytes_chunk(buf[o:o + step], fmt, channels)
             for o in range(0, len(buf), step)]
    return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)


def raw_frame_count(path, fmt, channels):
    """Число кадров (по всем каналам) в raw-файле; ошибка при некратной длине."""
    frame = BYTES[fmt] * int(channels)
    size = os.path.getsize(path)
    if size % frame:
        raise ValueError(f"файл {path}: длина {size} не кратна кадру {frame}")
    return size // frame


class StreamPcmReader:
    """Случайное чтение raw-PCM окна float64 без загрузки файла целиком."""

    def __init__(self, path, fmt, channels):
        self.path = str(path)
        self.fmt = fmt
        self.channels = int(channels)
        self.frame_bytes = BYTES[fmt] * self.channels
        self.frames = raw_frame_count(path, fmt, self.channels)

    def read_frames(self, start, count):
        if start < 0 or count < 0 or start + count > self.frames:
            raise ValueError("запрос за пределами записи")
        out = np.empty((count, self.channels), dtype=np.float64)
        step_frames = _chunk_bytes(self.fmt, self.channels) // self.frame_bytes
        with open(self.path, "rb") as fh:
            fh.seek(start * self.frame_bytes)
            pos = 0
            while pos < count:
                take = min(step_frames, count - pos)
                buf = fh.read(take * self.frame_bytes)
                if len(buf) != take * self.frame_bytes:
                    raise IOError("неожиданный конец файла")
                out[pos:pos + take] = _decode_bytes_chunk(buf, self.fmt, self.channels)
                pos += take
        return out


def stream_to_float32_wav(src, fmt, channels, dst, fs):
    """Один проход raw-PCM → float32 WAV (IEEE float); память ограничена блоком.

    Возвращает число кадров. Для S24_3LE и S16_LE хранение во float32 точно
    (мантисса 24 бита покрывает разрядность сэмплов); для S32_LE появляется
    округление до ~2^-25 FS (−150 dBFS) — несущественно для этих измерений,
    но отметьте это при работе с 32-битными АЦП.
    """
    channels = int(channels)
    frames = raw_frame_count(src, fmt, channels)
    data_size = frames * channels * 4
    block_align = channels * 4
    header = (b"RIFF" + (36 + data_size).to_bytes(4, "little") + b"WAVE"
              + b"fmt " + (16).to_bytes(4, "little")
              + (3).to_bytes(2, "little")                       # IEEE float
              + channels.to_bytes(2, "little")
              + int(fs).to_bytes(4, "little")
              + int(fs * block_align).to_bytes(4, "little")
              + block_align.to_bytes(2, "little")
              + (32).to_bytes(2, "little")
              + b"data" + data_size.to_bytes(4, "little"))
    if len(header) != FLOAT32_WAV_HEADER:
        raise RuntimeError("внутренняя ошибка WAV-заголовка")
    step = _chunk_bytes(fmt, channels)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(header)
        while True:
            buf = fin.read(step)
            if not buf:
                break
            fout.write(_decode_bytes_chunk(buf, fmt, channels).astype("<f4").tobytes())
    return frames
