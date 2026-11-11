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


def make_generator_signal(fs: int, waveform: str = "sine", frequency_hz: float = 1000.0,
                          level_dbfs: float = -6.0, seconds: float = 1.0,
                          noise_low_hz: float = 20.0, noise_high_hz: float = 20000.0,
                          seed: int = 12345):
    """Build a loopable two-purpose generator buffer and truthful level metadata.

    Periodic waveforms use a Nyquist-limited Fourier series (up to 99 harmonics)
    and are peak-normalized to the requested dBFS. Noise level is RMS dBFS and
    follows make_test_noise()'s -3 dBFS peak safety limit.
    """
    fs = int(fs)
    seconds = float(seconds)
    frequency_hz = float(frequency_hz)
    level_dbfs = float(level_dbfs)
    if fs < 8000 or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("нужна положительная длительность и Fs не ниже 8 кГц")
    if not math.isfinite(level_dbfs) or not -120.0 <= level_dbfs <= 0.0:
        raise ValueError("уровень генератора должен быть от -120 до 0 dBFS")
    n = max(4096, int(round(fs * seconds)))
    if waveform in ("white", "pink"):
        x, info = make_test_noise(fs, n / fs, kind=waveform, rms_dbfs=level_dbfs,
                                  f_lo=noise_low_hz, f_hi=noise_high_hz, seed=seed)
        info.update({"waveform": waveform, "frequency_hz": None,
                     "level_definition": "rms_dbfs"})
        return x, info

    if waveform not in ("sine", "triangle", "square", "sawtooth"):
        raise ValueError(f"неизвестная форма сигнала: {waveform}")
    if not math.isfinite(frequency_hz) or frequency_hz <= 0.0 or frequency_hz >= 0.45 * fs:
        raise ValueError(f"частота должна быть в пределах 0…{0.45 * fs:.0f} Гц")

    t = np.arange(n, dtype=np.float64) / fs
    phase = 2.0 * np.pi * frequency_hz * t
    if waveform == "sine":
        x = np.sin(phase)
    else:
        # Keep the spectrum below Nyquist. The cap avoids expensive loops for
        # very low frequencies while preserving the visible shape well.
        hmax = min(99, int((0.45 * fs) // frequency_hz))
        if hmax < 1:
            raise ValueError("частота слишком высока для этой формы сигнала")
        x = np.zeros(n, dtype=np.float64)
        if waveform in ("square", "triangle"):
            harmonics = range(1, hmax + 1, 2)
        else:
            harmonics = range(1, hmax + 1)
        for h in harmonics:
            if waveform == "square":
                coefficient = 1.0 / h
            elif waveform == "triangle":
                coefficient = ((-1.0) ** ((h - 1) // 2)) / (h * h)
            else:  # sawtooth
                coefficient = ((-1.0) ** (h + 1)) / h
            x += coefficient * np.sin(h * phase)
        peak = float(np.max(np.abs(x)))
        if peak <= 0 or not math.isfinite(peak):
            raise ValueError("не удалось сформировать периодический сигнал")
        x /= peak

    peak_target = 10.0 ** (level_dbfs / 20.0)
    x *= peak_target
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x * x)))
    info = {
        "waveform": waveform, "fs": fs, "seconds": n / fs,
        "frequency_hz": frequency_hz, "requested_level_dbfs": level_dbfs,
        "level_definition": "peak_dbfs", "peak_dbfs": 20.0 * math.log10(max(peak, 1e-30)),
        "rms_dbfs": 20.0 * math.log10(max(rms * math.sqrt(2.0), 1e-30)),
        "harmonics_generated": 1 if waveform == "sine" else hmax,
        "band_limit_hz": 0.45 * fs,
    }
    return x.astype(np.float64), info
