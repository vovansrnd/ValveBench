"""Тестовые сигналы: синусы, white/pink noise и inverse-RIAA shaped noise."""
from __future__ import annotations

import math
import numpy as np


def make_test_noise(fs: int, seconds: float, kind: str = "pink", rms_dbfs: float = -24.0,
                    f_lo: float = 10.0, f_hi: float = 20000.0, seed: int | None = 12345,
                    riaa_shape=None):
    """Вернуть (signal, info). RMS задан в dBFS по соглашению полного синуса.

    kind: white, pink или riaa. В riaa режиме шум предыскажён обратной
    playback-RIAA, чтобы выход исправного фонокорректора был приблизительно ровным.
    Пиковый запас ограничивается до -3 dBFS; фактический RMS отражается в info.
    """
    fs = int(fs)
    n = max(4096, int(round(float(seconds) * fs)))
    rng = np.random.default_rng(seed)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    z = rng.normal(size=f.size) + 1j * rng.normal(size=f.size)
    weight = np.ones_like(f)
    valid = (f >= max(1.0, f_lo)) & (f <= min(f_hi, 0.45 * fs))
    if kind == "pink":
        weight[valid] = 1.0 / np.sqrt(f[valid])
    elif kind == "riaa":
        if riaa_shape is None:
            raise ValueError("для riaa noise нужна функция обратной RIAA-кривой")
        weight[valid] = np.asarray(riaa_shape(f[valid]), dtype=float)
    elif kind != "white":
        raise ValueError(f"неизвестный тип шума: {kind}")
    weight[~valid] = 0.0
    spec = z * weight
    spec[0] = 0.0
    if n % 2 == 0:
        spec[-1] = spec[-1].real + 0j
    x = np.fft.irfft(spec, n=n)
    x -= np.mean(x)
    target_rms = 10.0 ** (float(rms_dbfs) / 20.0) / math.sqrt(2.0)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 0:
        raise ValueError("не удалось сформировать тестовый шум")
    x *= target_rms / rms
    peak_limit = 10.0 ** (-3.0 / 20.0)
    peak = float(np.max(np.abs(x)))
    scale_limited = peak > peak_limit
    if scale_limited:
        x *= peak_limit / peak
    actual_rms = float(np.sqrt(np.mean(x * x)))
    actual_dbfs = 20.0 * math.log10(max(actual_rms * math.sqrt(2.0), 1e-20))
    info = {
        "kind": kind, "fs": fs, "seconds": n / fs, "requested_rms_dbfs": float(rms_dbfs),
        "actual_rms_dbfs": actual_dbfs, "peak_dbfs": 20.0 * math.log10(max(float(np.max(np.abs(x))), 1e-20)),
        "peak_limited": scale_limited, "f_lo": float(f_lo), "f_hi": float(min(f_hi, 0.45 * fs)),
    }
    return x.astype(np.float64), info
