"""Длительный matched-level тест ЦАП -> аналоговый тракт -> АЦП.

Отличие от прежних ad-hoc скриптов:
* отдельные playback и capture ALSA PCM (можно выбрать разные карты);
* проверка поддерживаемого PCM-формата до основного запуска;
* частота тона сначала ищется FFT, затем уточняется только в пределах половины
  интерполированного FFT-бина — без многолокального поиска по широкому интервалу;
* в каждом 100-мс окне DC и гармоники H1..Hn вычитаются LS-проекцией;
* правый и левый каналы анализируются одинаково;
* сохраняются summary, CSV и float WAV.

Это сравнительный инженерный тест, а не метрологическая сертификация.
"""
from __future__ import annotations

import csv
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import fft as sp_fft
from scipy.optimize import minimize_scalar
from scipy.signal.windows import blackmanharris, hann

from .pcm import (FORMATS, to_pcm, from_pcm, stream_to_float32_wav,
                  FLOAT32_WAV_HEADER)
from . import io_health as io_health_mod


@dataclass
class DeviceFormat:
    device: str
    fmt: str
    channels: int = 2


def _run_probe(cmd, *, input_bytes=None, timeout=6.0):
    try:
        return subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Не найдена команда {cmd[0]!r}; установи alsa-utils") from exc
    except subprocess.TimeoutExpired as exc:
        # Для probe playback timeout означает, что ALSA открыл PCM, но тестовый
        # процесс не завершился штатно. Это не считается успешной проверкой.
        return subprocess.CompletedProcess(cmd, 124, b"", b"timeout")


def _probe_play(dev: str, fs: int, fmt: str, channels: int = 2) -> tuple[bool, str]:
    n = max(256, int(fs * 0.08))
    payload = to_pcm(np.zeros((n, channels), dtype=np.float64), fmt)
    cmd = ["aplay", "-q", "-D", dev, "-t", "raw", "-f", fmt,
           "-r", str(fs), "-c", str(channels), "-"]
    p = _run_probe(cmd, input_bytes=payload, timeout=5.0)
    return p.returncode == 0, (p.stderr or p.stdout).decode(errors="replace").strip()


def _probe_capture(dev: str, fs: int, fmt: str, channels: int = 2) -> tuple[bool, str]:
    cmd = ["arecord", "-q", "-D", dev, "-t", "raw", "-f", fmt,
           "-r", str(fs), "-c", str(channels), "-d", "1", "/dev/null"]
    p = _run_probe(cmd, timeout=5.0)
    return p.returncode == 0, (p.stderr or p.stdout).decode(errors="replace").strip()


def choose_device_format(dev: str, fs: int, direction: str, requested: str = "auto") -> DeviceFormat:
    """Find PCM format by actually opening the requested ALSA hw PCM.

    Preference is 24-bit packed, then 24-in-32, 32-bit, and 16-bit. For a
    requested exact format, only that format is tried.
    """
    if direction not in ("playback", "capture"):
        raise ValueError("direction must be playback or capture")
    candidates = [requested] if requested != "auto" else ["S24_3LE", "S24_LE", "S32_LE", "S16_LE"]
    errors = []
    for fmt in candidates:
        if fmt not in FORMATS:
            raise ValueError(f"Неизвестный PCM-формат: {fmt}")
        ok, detail = (_probe_play(dev, fs, fmt) if direction == "playback"
                      else _probe_capture(dev, fs, fmt))
        if ok:
            return DeviceFormat(dev, fmt, 2)
        errors.append(f"{fmt}: {detail or 'не поддерживается'}")
    raise RuntimeError(f"{dev}: нет подходящего формата {direction} при {fs} Гц. " + " | ".join(errors))


def _write_raw(path: Path, samples: np.ndarray, fmt: str) -> None:
    path.write_bytes(to_pcm(samples, fmt))


def _capture_and_play(play_file: Path, capture_file: Path, play: DeviceFormat,
                      capture: DeviceFormat, fs: int, capture_seconds: float,
                      lead_seconds: float = 0.5) -> dict:
    """Запуск arecord + aplay; 0.1.12-p1: stderr обоих процессов пишется во
    временные файлы (pipe исключён — любой объём сообщений не блокирует
    процессы). Возвращает пути stderr-файлов для XRUN-контроля; удаляет каталог
    вызывающий код после чтения.
    """
    err_dir = Path(tempfile.mkdtemp(prefix="valvebench-io-"))
    rec_err_path = err_dir / "arecord.stderr"
    play_err_path = err_dir / "aplay.stderr"
    rec_cmd = ["arecord", "-q", "-D", capture.device, "-t", "raw", "-f", capture.fmt,
               "-r", str(fs), "-c", str(capture.channels), "-d",
               str(max(1, int(math.ceil(capture_seconds))),), str(capture_file)]
    with rec_err_path.open("w") as rec_err, play_err_path.open("w") as play_err:
        rec = subprocess.Popen(rec_cmd, stdout=subprocess.DEVNULL, stderr=rec_err, text=True)
        try:
            time.sleep(lead_seconds)
            play_cmd = ["aplay", "-q", "-D", play.device, "-t", "raw", "-f", play.fmt,
                        "-r", str(fs), "-c", str(play.channels), str(play_file)]
            p = subprocess.run(play_cmd, stdout=subprocess.DEVNULL, stderr=play_err,
                               timeout=capture_seconds + 10.0)
            if p.returncode:
                raise RuntimeError(f"Ошибка воспроизведения {play.device} ({play.fmt}): "
                                   + play_err_path.read_text(errors="replace")[-400:].strip())
            rec.communicate(timeout=capture_seconds + 10.0)
            if rec.returncode:
                raise RuntimeError(f"Ошибка записи {capture.device} ({capture.fmt}): "
                                   + rec_err_path.read_text(errors="replace")[-400:].strip())
        except Exception:
            if rec.poll() is None:
                rec.terminate()
                try:
                    rec.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    rec.kill()
                    rec.wait(timeout=2.0)
            raise
    return {"capture_stderr": str(rec_err_path), "playback_stderr": str(play_err_path),
            "err_dir": str(err_dir)}


def _tone_chunk(start: int, count: int, fs: int, f: float, amplitude: float,
                left: bool, right: bool, fade: np.ndarray | None = None) -> np.ndarray:
    idx = np.arange(start, start + count, dtype=np.float64)
    mono = amplitude * np.sin(2.0 * np.pi * f * idx / fs)
    if fade is not None:
        mono *= fade
    out = np.zeros((count, 2), dtype=np.float64)
    if left:
        out[:, 0] = mono
    if right:
        out[:, 1] = mono
    return out


def _write_test_program(path: Path, fmt: str, fs: int, f: float, level_dbfs: float,
                        duration_s: float = 40.0, idle_s: float = 15.0,
                        gap_s: float = 8.0, fade_s: float = 1.0) -> float:
    """Write a continuous L-only/R-only program with long digital-silence sections.

    Long pre/post idle periods make the noise baseline less vulnerable to rare
    events. The function returns the exact playback duration.
    """
    duration_s, idle_s, gap_s, fade_s = map(float, (duration_s, idle_s, gap_s, fade_s))
    if duration_s < 5 or idle_s < 3 or gap_s < 2 or fade_s < 0.1:
        raise ValueError("Требуются duration≥5 s, idle≥3 s, gap≥2 s, fade≥0.1 s")
    amp = 10.0 ** (level_dbfs / 20.0)
    l_fade_start = idle_s
    l_steady_start = l_fade_start + fade_s
    l_steady_end = l_steady_start + duration_s
    l_fade_end = l_steady_end + fade_s
    r_fade_start = l_fade_end + gap_s
    r_steady_start = r_fade_start + fade_s
    r_steady_end = r_steady_start + duration_s
    r_fade_end = r_steady_end + fade_s
    play_seconds = r_fade_end + idle_s
    total = int(round(play_seconds * fs))
    chunk = fs
    with path.open("wb") as fh:
        for start in range(0, total, chunk):
            count = min(chunk, total - start)
            local = np.arange(start, start + count, dtype=np.float64) / fs
            left = np.zeros(count, dtype=np.float64)
            right = np.zeros(count, dtype=np.float64)
            idx_l = (local >= l_fade_start) & (local < l_fade_end)
            idx_r = (local >= r_fade_start) & (local < r_fade_end)
            if np.any(idx_l):
                tt = local[idx_l]
                env = np.ones(tt.size, dtype=np.float64)
                fi = tt < l_steady_start
                fo = tt >= l_steady_end
                env[fi] = (tt[fi] - l_fade_start) / fade_s
                env[fo] = (l_fade_end - tt[fo]) / fade_s
                left[idx_l] = amp * env * np.sin(2.0 * np.pi * f * tt)
            if np.any(idx_r):
                tt = local[idx_r]
                env = np.ones(tt.size, dtype=np.float64)
                fi = tt < r_steady_start
                fo = tt >= r_steady_end
                env[fi] = (tt[fi] - r_fade_start) / fade_s
                env[fo] = (r_fade_end - tt[fo]) / fade_s
                right[idx_r] = amp * env * np.sin(2.0 * np.pi * f * tt)
            fh.write(to_pcm(np.column_stack((left, right)), fmt))
    return play_seconds


def _tone_fit(y: np.ndarray, fs: int, nominal_hz: float) -> tuple[float, float]:
    """Return (frequency Hz, sine peak dBFS) with a single-lobe bounded refinement.

    The old script optimized across nominal±0.5 Hz on a 40-s recording. That
    interval contains many correlation maxima spaced 1/T apart; bounded search
    could converge to a neighbouring lobe. Here the FFT peak is found first and
    refinement is confined to the central Hann lobe around that peak.
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim != 1 or len(y) < int(fs * 0.5):
        raise ValueError("Для оценки тона нужно не менее 0,5 с моно-записи")
    y = y - float(np.mean(y))
    n = len(y)
    w = hann(n, sym=False)
    # 8x zero-padding is enough to identify the main lobe; refine inside
    # a narrow interval that remains well inside its first nulls. This keeps
    # peak search memory moderate on older laptops at 192/384 kHz.
    nfft = sp_fft.next_fast_len(n * 8)
    mag = np.abs(sp_fft.rfft(y * w, n=nfft))
    freqs = sp_fft.rfftfreq(nfft, 1.0 / fs)
    search = (freqs >= nominal_hz - 5.0) & (freqs <= nominal_hz + 5.0)
    if not np.any(search):
        raise ValueError("частота тона вне диапазона поиска")
    indices = np.flatnonzero(search)
    peak_i = int(indices[np.argmax(mag[search])])
    guess = float(freqs[peak_i])
    refine_half_width = fs / nfft * 4.0
    lo, hi = max(nominal_hz - 5.0, guess - refine_half_width), min(nominal_hz + 5.0, guess + refine_half_width)
    # Four padded bins cover the FFT peak uncertainty while remaining inside
    # the Hann main lobe (first null is about 1/T from its centre).
    sample_idx = np.arange(n, dtype=np.float64)
    yy = y

    def fit_at(fq: float) -> tuple[float, float, float]:
        phase = 2.0 * np.pi * float(fq) * sample_idx / fs
        s = np.sin(phase)
        c = np.cos(phase)
        ss = float(np.dot(s, s)); cc = float(np.dot(c, c)); sc = float(np.dot(s, c))
        ys = float(np.dot(yy, s)); yc = float(np.dot(yy, c))
        det = ss * cc - sc * sc
        if det <= 1e-20:
            return 0.0, 0.0, 0.0
        a = (ys * cc - yc * sc) / det
        b = (yc * ss - ys * sc) / det
        return a, b, a * ys + b * yc

    def objective(fq: float) -> float:
        return -fit_at(fq)[2]

    opt = minimize_scalar(objective, bounds=(lo, hi), method="bounded",
                          options={"xatol": 1e-11, "maxiter": 80})
    f_est = float(opt.x)
    a, b, _ = fit_at(f_est)
    peak = math.hypot(a, b)
    return f_est, 20.0 * math.log10(max(peak, 1e-30))


def _tone_level_at(y: np.ndarray, fs: int, frequency_hz: float) -> float:
    """Coherent sine projection at a known frequency; used for channel mapping.

    This is not a broadband noise estimate. It answers how much of the known
    test tone is present in a given ADC channel.
    """
    y = np.asarray(y, dtype=np.float64)
    y = y - float(np.mean(y))
    t = np.arange(y.size, dtype=np.float64) / fs
    phase = 2.0 * np.pi * float(frequency_hz) * t
    s, c = np.sin(phase), np.cos(phase)
    ss, cc, sc = float(s @ s), float(c @ c), float(s @ c)
    ys, yc = float(y @ s), float(y @ c)
    det = ss * cc - sc * sc
    if det <= 1e-20:
        return -300.0
    a = (ys * cc - yc * sc) / det
    b = (yc * ss - ys * sc) / det
    return 20.0 * math.log10(max(math.hypot(a, b), 1e-30))


def _guard_steady_ranges(state_ranges: dict[str, tuple[float, float]], guard_s: float) -> dict[str, tuple[float, float]]:
    """Move active analysis bounds away from the playback fade-in/fade-out edges.

    This does not shorten the generated test signal. It only excludes windows
    whose analysis aperture could overlap an envelope transition or timing
    uncertainty at the boundary.
    """
    guard_s = max(0.0, float(guard_s))
    guarded = dict(state_ranges)
    for state in ("L_steady", "R_steady"):
        if state not in guarded:
            continue
        start, end = guarded[state]
        if end - start <= 2.0 * guard_s:
            raise ValueError(f"Недостаточно длительный {state} после защитных интервалов")
        guarded[state] = (start + guard_s, end - guard_s)
    return guarded


def _build_steady_state_ranges(lead_s: float, idle_s: float, duration_s: float,
                               gap_s: float, fade_s: float, window_s: float) -> tuple[dict[str, tuple[float, float]], float]:
    """Build capture-time state ranges and guard active states from fade edges."""
    l_start = idle_s + fade_s
    l_end = l_start + duration_s
    gap_start = l_end + fade_s  # end of L fade-out
    r_fade_start = gap_start + gap_s
    r_start = r_fade_start + fade_s  # end of R fade-in
    r_end = r_start + duration_s
    post_start = r_end + fade_s
    ranges = {
        "idle_pre": (lead_s + 0.5, lead_s + idle_s - 0.5),
        "L_steady": (lead_s + l_start, lead_s + l_end),
        # Do not include the final fade-in of R in idle_gap.
        "idle_gap": (lead_s + gap_start + 0.5, lead_s + r_fade_start - 0.5),
        "R_steady": (lead_s + r_start, lead_s + r_end),
        "idle_post": (lead_s + post_start + 0.5, lead_s + post_start + idle_s - 0.5),
    }
    guard_s = max(0.25, min(1.0, window_s * 2.0))
    return _guard_steady_ranges(ranges, guard_s), guard_s


def _tone_continuity_track(y: np.ndarray, fs: int, state: str, channel: str,
                           start_s: float, end_s: float, frequency_hz: float,
                           reference_dbfs: float, subwindow_ms: float = 10.0,
                           threshold_below_db: float = 20.0,
                           min_episode_ms: float = 20.0) -> tuple[dict, list[dict]]:
    """Track presence of the expected carrier in short, contiguous subwindows.

    The residual/noise analysis removes H1..Hn and therefore cannot distinguish
    an additive disturbance from a missing carrier. This independent estimator
    fits only the fundamental in short windows and flags a dropout when it stays
    at least `threshold_below_db` below the measured steady carrier reference.
    """
    # 0.1.12-P0: y может быть memmap-колонкой float32 — вся запись в float64
    # не материализуется, окна конвертируются в _tone_level_at.
    if subwindow_ms <= 0 or threshold_below_db <= 0 or min_episode_ms <= 0:
        raise ValueError("Параметры контроля несущей должны быть положительными")
    # Keep at least five cycles in each fit. At the default 997 Hz this is 10 ms;
    # at low tone frequencies the window expands, up to 100 ms.
    sub_s = max(float(subwindow_ms) / 1000.0, 5.0 / float(frequency_hz))
    if sub_s > 0.100:
        return ({"available": False, "reason": "частота тона слишком низкая для короткого контроля несущей",
                 "subwindow_ms": sub_s * 1000.0, "reference_dbfs": float(reference_dbfs),
                 "dropout_episodes": 0, "total_dropout_ms": 0.0, "longest_dropout_ms": 0.0}, [])
    n = max(16, int(round(sub_s * fs)))
    actual_sub_ms = n / fs * 1000.0
    start = max(0, int(round(float(start_s) * fs)))
    end = min(y.size, int(round(float(end_s) * fs)))
    threshold_dbfs = float(reference_dbfs) - float(threshold_below_db)
    samples: list[dict] = []
    for off in range(start, max(start, end - n + 1), n):
        chunk = y[off:off + n]
        if chunk.size != n:
            continue
        level = _tone_level_at(chunk, fs, frequency_hz)
        start_time = off / fs
        samples.append({
            "start_s": start_time,
            "time_s": start_time + (n / fs) / 2.0,
            "state": state,
            "channel": channel,
            "tone_level_dbfs": float(level),
            "relative_to_reference_db": float(level - reference_dbfs),
            "below_threshold": bool(level <= threshold_dbfs),
            "dropout_episode_id": None,
        })

    # Group contiguous low-carrier subwindows; isolated one-window dips are
    # retained in the raw track but are not promoted to physical dropout events.
    low = [r for r in samples if r["below_threshold"]]
    groups: list[list[dict]] = []
    if low:
        current = [low[0]]
        step_s = n / fs
        for row in low[1:]:
            if row["start_s"] - current[-1]["start_s"] <= step_s * 1.51:
                current.append(row)
            else:
                groups.append(current)
                current = [row]
        groups.append(current)

    episodes = []
    for group in groups:
        duration_ms = len(group) * actual_sub_ms
        if duration_ms + 1e-6 < min_episode_ms:
            continue
        episode_id = len(episodes) + 1
        for row in group:
            row["dropout_episode_id"] = episode_id
        episodes.append({
            "id": episode_id,
            "start_s": float(group[0]["start_s"]),
            "end_s": float(group[-1]["start_s"] + n / fs),
            "duration_ms": float(duration_ms),
            "min_tone_dbfs": float(min(r["tone_level_dbfs"] for r in group)),
            "mean_tone_dbfs": float(np.mean([r["tone_level_dbfs"] for r in group])),
        })

    levels = np.asarray([r["tone_level_dbfs"] for r in samples], dtype=np.float64)
    confirmed_sample_count = sum(len(g) for g in groups if len(g) * actual_sub_ms + 1e-6 >= min_episode_ms)
    result = {
        "available": True,
        "channel": channel,
        "reference_dbfs": float(reference_dbfs),
        "threshold_dbfs": float(threshold_dbfs),
        "threshold_below_reference_db": float(threshold_below_db),
        "subwindow_ms": float(actual_sub_ms),
        "min_episode_ms": float(min_episode_ms),
        "subwindows": int(len(samples)),
        "median_tone_dbfs": float(np.median(levels)) if levels.size else None,
        "p01_tone_dbfs": float(np.percentile(levels, 1)) if levels.size else None,
        "min_tone_dbfs": float(np.min(levels)) if levels.size else None,
        "below_threshold_subwindows": int(sum(r["below_threshold"] for r in samples)),
        "confirmed_dropout_subwindows": int(confirmed_sample_count),
        "dropout_episodes": int(len(episodes)),
        "total_dropout_ms": float(sum(e["duration_ms"] for e in episodes)),
        "longest_dropout_ms": float(max((e["duration_ms"] for e in episodes), default=0.0)),
        "episodes": episodes,
    }
    return result, samples


def _calibration_trial(outdir: Path, trial: int, play: DeviceFormat, capture: DeviceFormat,
                       fs: int, tone_hz: float, tone_level: float) -> tuple[float, float]:
    play_file = outdir / f"calibration-{trial}.raw"
    cap_file = outdir / f"calibration-capture-{trial}.raw"
    n = int(round(2.5 * fs))
    t = np.arange(n, dtype=np.float64) / fs
    amp = 10.0 ** (tone_level / 20.0)
    signal = amp * np.sin(2.0 * np.pi * tone_hz * t)
    _write_raw(play_file, np.column_stack((signal, signal)), play.fmt)
    try:
        _capture_and_play(play_file, cap_file, play, capture, fs, 3.5, lead_seconds=0.45)
        raw = cap_file.read_bytes()
        y = from_pcm(raw, capture.fmt, 2)
        # Stable one-second central section; avoid process start/stop transients.
        a = int(round(1.1 * fs)); b = int(round(2.1 * fs))
        if len(y) < b:
            raise RuntimeError("ALSA capture короче калибровочного участка")
        l = _tone_fit(y[a:b, 0], fs, tone_hz)[1]
        r = _tone_fit(y[a:b, 1], fs, tone_hz)[1]
        return l, r
    finally:
        for path in (play_file, cap_file):
            try:
                path.unlink()
            except OSError:
                pass


def _make_projection(n: int, fs: int, f0: float, max_harmonics: int) -> np.ndarray:
    """Orthonormal basis for per-window LS removal of DC and harmonic tones."""
    t = np.arange(n, dtype=np.float64) / fs
    cols = [np.ones(n, dtype=np.float64)]
    max_h = min(int(max_harmonics), int(20000.0 // f0), int((fs / 2.0 * 0.98) // f0))
    for h in range(1, max_h + 1):
        ph = 2.0 * np.pi * h * f0 * t
        cols.extend((np.sin(ph), np.cos(ph)))
    design = np.column_stack(cols)
    q, _ = np.linalg.qr(design, mode="reduced")
    return q


def _ultrasonic_band_edges(fs: int) -> list[tuple[str, float, float]]:
    """Return fixed 10-kHz bands clipped to the actual Nyquist frequency."""
    nyquist = float(fs) / 2.0
    out = []
    def tag_khz(value: float) -> str:
        khz = value / 1000.0
        if math.isclose(khz, round(khz), rel_tol=0.0, abs_tol=1e-9):
            return str(int(round(khz)))
        return f"{khz:g}".replace(".", "p")
    for lo, nominal_hi in ((40000, 50000), (50000, 60000), (60000, 70000),
                           (70000, 80000), (80000, 90000), (90000, 96000)):
        hi = min(float(nominal_hi), nyquist)
        if hi > lo:
            label = f"{tag_khz(float(lo))}_{tag_khz(hi)}k"
            out.append((label, float(lo), hi))
    return out


def _window_spectrum(x: np.ndarray, fs: int, window: np.ndarray):
    """One-sided power spectral density in mean-square/Hz."""
    n = len(x)
    spec = np.fft.rfft(np.asarray(x, dtype=np.float64) * window)
    psd = np.abs(spec) ** 2 / (fs * float(np.sum(window ** 2)))
    if n > 1:
        if n % 2 == 0:
            psd[1:-1] *= 2.0
        else:
            psd[1:] *= 2.0
    return np.fft.rfftfreq(n, 1.0 / fs), psd, fs / n


def _band_metrics(freqs: np.ndarray, psd: np.ndarray, df: float,
                  lo: float, hi: float) -> dict | None:
    """Integrated level plus median and 95th-percentile ASD in a band.

    ASD is expressed as dBFS/sqrt(Hz), with the same peak-sine dBFS
    convention used by the rest of ValveBench. The equivalent ASD is the
    integrated band level divided by the square root of the occupied bandwidth.
    """
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask) and math.isclose(hi, float(freqs[-1]), rel_tol=0.0, abs_tol=df * 0.51):
        mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return None
    vals = np.maximum(psd[mask], 1e-300)
    width = float(np.count_nonzero(mask) * df)
    power = float(np.sum(vals) * df)
    band_dbfs = 10.0 * math.log10(max(2.0 * power, 1e-300))
    median_asd = 10.0 * math.log10(max(2.0 * float(np.median(vals)), 1e-300))
    p95_asd = 10.0 * math.log10(max(2.0 * float(np.percentile(vals, 95)), 1e-300))
    equiv_asd = band_dbfs - 10.0 * math.log10(max(width, 1e-300))
    return {"dbfs": band_dbfs, "asd_median_dbfs_sqrt_hz": median_asd,
            "asd_p95_dbfs_sqrt_hz": p95_asd,
            "equivalent_asd_dbfs_sqrt_hz": equiv_asd,
            "bins": int(np.count_nonzero(mask)), "bandwidth_hz": width}


def _band_dbfs(x: np.ndarray, fs: int, lo: float, hi: float, window: np.ndarray) -> float:
    """Compatibility helper used by tests and older report code."""
    freqs, psd, df = _window_spectrum(x, fs, window)
    metric = _band_metrics(freqs, psd, df, lo, min(hi, fs / 2.0))
    return float(metric["dbfs"]) if metric is not None else float("nan")


def _burst_episodes(rows: list[dict], threshold_dbfs: float, hop_s: float) -> dict:
    """Group adjacent threshold windows into episodes; windows are not events."""
    selected = sorted((r for r in rows if r["residual_20_20k_dbfs"] > threshold_dbfs),
                      key=lambda r: r["time_s"])
    if not selected:
        return {"windows": 0, "episodes": 0, "longest_s": 0.0, "peak_dbfs": None}
    groups = []
    current = [selected[0]]
    for row in selected[1:]:
        if row["time_s"] - current[-1]["time_s"] <= hop_s * 1.51:
            current.append(row)
        else:
            groups.append(current)
            current = [row]
    groups.append(current)
    return {"windows": len(selected), "episodes": len(groups),
            "longest_s": max(len(group) * hop_s for group in groups),
            "peak_dbfs": max(r["residual_20_20k_dbfs"] for r in selected)}


def _analyse_channel(y: np.ndarray, fs: int, state_ranges: dict[str, tuple[float, float]],
                     channel: str, f0: float | dict[str, float], target_dbfs: float, window_seconds: float,
                     hop_seconds: float, max_harmonics: int) -> tuple[list[dict], dict]:
    n = int(round(window_seconds * fs))
    hop = int(round(hop_seconds * fs))
    if n < 1024 or hop < 1:
        raise ValueError("Слишком короткое окно анализа")
    win = blackmanharris(n, sym=False)
    q_cache = {}
    out = []
    baseline_pre_values = []
    baseline_post_values = []
    band_edges = _ultrasonic_band_edges(fs)
    for state, (start_s, end_s) in state_ranges.items():
        start = int(round(start_s * fs)); end = min(len(y), int(round(end_s * fs)))
        for off in range(start, max(start, end - n + 1), hop):
            raw = np.asarray(y[off:off + n], dtype=np.float64)
            if raw.size != n:
                continue
            t_mid = (off + n / 2.0) / fs
            if state.startswith("idle"):
                residual = raw - float(np.mean(raw))
                removed_harmonics = 0
                f_state = None
            else:
                f_state = float(f0.get(state, f0.get("default"))) if isinstance(f0, dict) else float(f0)
                if f_state not in q_cache:
                    q_cache[f_state] = _make_projection(n, fs, f_state, max_harmonics)
                q = q_cache[f_state]
                coeff = q.T @ raw
                residual = raw - q @ coeff
                removed_harmonics = int((q.shape[1] - 1) // 2)
            lev20 = _band_dbfs(residual, fs, 20.0, 20000.0, win)
            freqs, psd, df = _window_spectrum(raw, fs, win)
            raw_bands = {label: _band_metrics(freqs, psd, df, lo, hi)
                         for label, lo, hi in band_edges}
            valid_band_power = [v["dbfs"] for v in raw_bands.values() if v is not None]
            lev40 = (10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in valid_band_power))
                     if valid_band_power else None)
            row = {"time_s": t_mid, "state": state, "channel": channel,
                   "residual_20_20k_dbfs": lev20, "raw_40_nyquist_dbfs": lev40,
                   "raw_ultrasonic_bands": raw_bands,
                   "residual_removed_harmonics": removed_harmonics,
                   "projection_frequency_hz": f_state}
            out.append(row)
            if state == "idle_pre":
                baseline_pre_values.append(lev20)
            elif state == "idle_post":
                baseline_post_values.append(lev20)

    baseline_values = baseline_pre_values + baseline_post_values
    base_pre = float(np.median(baseline_pre_values)) if baseline_pre_values else float("nan")
    base_post = float(np.median(baseline_post_values)) if baseline_post_values else None
    base = float(np.median(baseline_values)) if baseline_values else float("nan")
    drift = (base_post - base_pre) if base_post is not None and math.isfinite(base_pre) else None
    idle_band_summary = {}
    for label, _lo, _hi in band_edges:
        idle_band_summary[label] = {}
        for idle_state in ("idle_pre", "idle_post"):
            idle_rows = [r for r in out if r["state"] == idle_state]
            band_values = [r["raw_ultrasonic_bands"].get(label) for r in idle_rows]
            band_values = [v for v in band_values if v is not None]
            idle_band_summary[label][idle_state] = (
                {"dbfs_median": float(np.median([v["dbfs"] for v in band_values])),
                 "asd_median_dbfs_sqrt_hz": float(np.median([v["asd_median_dbfs_sqrt_hz"] for v in band_values]))}
                if band_values else None)
    summary = {"baseline_dbfs": base, "baseline_pre_dbfs": base_pre,
               "baseline_post_dbfs": base_post, "baseline_drift_db": drift,
               "baseline_stable": (abs(drift) <= 3.0 if drift is not None else None),
               "idle_ultrasonic_bands": idle_band_summary, "states": {}}
    for state in ("L_steady", "R_steady"):
        vals = [r for r in out if r["state"] == state]
        v20 = np.asarray([r["residual_20_20k_dbfs"] for r in vals], dtype=float)
        v40_values = [r["raw_40_nyquist_dbfs"] for r in vals if r["raw_40_nyquist_dbfs"] is not None]
        v40 = np.asarray(v40_values, dtype=float)
        if not v20.size:
            continue
        burst_info = _burst_episodes(vals, base + 6.0, hop_seconds)
        band_summary = {}
        for label, _lo, _hi in band_edges:
            bvalues = [r["raw_ultrasonic_bands"].get(label) for r in vals]
            bvalues = [v for v in bvalues if v is not None]
            if not bvalues:
                band_summary[label] = None
                continue
            band_levels = np.asarray([v["dbfs"] for v in bvalues], dtype=float)
            asd_median = np.asarray([v["asd_median_dbfs_sqrt_hz"] for v in bvalues], dtype=float)
            asd_p95 = np.asarray([v["asd_p95_dbfs_sqrt_hz"] for v in bvalues], dtype=float)
            band_summary[label] = {
                "dbfs_median": float(np.median(band_levels)),
                "dbfs_p95": float(np.percentile(band_levels, 95)),
                "dbfs_max": float(np.max(band_levels)),
                "asd_median_dbfs_sqrt_hz": float(np.median(asd_median)),
                "asd_p95_dbfs_sqrt_hz": float(np.median(asd_p95)),
            }
        summary["states"][state] = {
            "windows": int(v20.size),
            "residual_20_20k": {"median": float(np.median(v20)), "p95": float(np.percentile(v20, 95)),
                                "max": float(np.max(v20)), "bursts": burst_info["windows"],
                                "episodes": burst_info["episodes"],
                                "longest_episode_s": burst_info["longest_s"],
                                "threshold_dbfs": base + 6.0},
            "raw_40_nyquist": ({"median": float(np.median(v40)), "p95": float(np.percentile(v40, 95)),
                                "max": float(np.max(v40))} if v40.size else None),
            "raw_ultrasonic_bands": band_summary,
        }
    return out, summary


def _shared_excursion_stats(rows: list[dict], summaries: dict, hop_s: float) -> dict:
    """Compare L/R residual excursions at matching times, state by state."""
    result = {}
    base_l = summaries["L"]["baseline_dbfs"]
    base_r = summaries["R"]["baseline_dbfs"]
    for state in ("L_steady", "R_steady"):
        by_channel = {
            ch: {round(r["time_s"], 6): r for r in rows
                 if r["state"] == state and r["channel"] == ch}
            for ch in ("L", "R")
        }
        common_times = sorted(set(by_channel["L"]) & set(by_channel["R"]))
        if not common_times:
            result[state] = {"windows": 0, "correlation_db": None,
                             "both_excursion_windows": 0, "either_excursion_windows": 0,
                             "shared_episodes": 0}
            continue
        lv = np.asarray([by_channel["L"][t]["residual_20_20k_dbfs"] for t in common_times])
        rv = np.asarray([by_channel["R"][t]["residual_20_20k_dbfs"] for t in common_times])
        corr = float(np.corrcoef(lv, rv)[0, 1]) if lv.size > 1 and np.std(lv) > 1e-12 and np.std(rv) > 1e-12 else None
        lm = lv > base_l + 6.0
        rm = rv > base_r + 6.0
        both = lm & rm
        either = lm | rm
        # Treat consecutive shared windows as one common-mode episode.
        shared_rows = [{"time_s": t, "residual_20_20k_dbfs": 1.0}
                       for t, flag in zip(common_times, both) if flag]
        shared = _burst_episodes(shared_rows, 0.0, hop_s)
        result[state] = {
            "windows": int(lv.size), "correlation_db": corr,
            "L_excursion_windows": int(np.count_nonzero(lm)),
            "R_excursion_windows": int(np.count_nonzero(rm)),
            "both_excursion_windows": int(np.count_nonzero(both)),
            "either_excursion_windows": int(np.count_nonzero(either)),
            "shared_episodes": int(shared["episodes"]),
        }
    return result


def compare_steady_runs(paths: list[str | Path]) -> tuple[str, list[dict]]:
    """Build one concise comparison table from previously saved steady.json files.

    Inputs may be JSON files or run directories. This does not claim that
    different physical pairings are the same instrument; the pair and settings
    remain explicit in every row.
    """
    rows = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            path = path / "steady.json"
        if not path.is_file():
            raise FileNotFoundError(f"Не найден steady.json: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        channel_map = data.get("channel_map", {})
        analysis = data.get("analysis", {})
        l_map = channel_map.get("PLAY_L_only", {})
        r_map = channel_map.get("PLAY_R_only", {})
        l_state = analysis.get("L", {}).get("states", {})
        r_state = analysis.get("R", {}).get("states", {})
        l_on_l = l_state.get("L_steady", {})
        r_on_r = r_state.get("R_steady", {})
        l_res = l_on_l.get("residual_20_20k", {})
        r_res = r_on_r.get("residual_20_20k", {})
        l_ultra = l_on_l.get("raw_40_nyquist", {}) or {}
        r_ultra = r_on_r.get("raw_40_nyquist", {}) or {}
        continuity = data.get("tone_continuity", {})
        l_carrier = continuity.get("L_steady", {}) or {}
        r_carrier = continuity.get("R_steady", {}) or {}
        row = {
            "report": str(path),
            "playback": data.get("playback_device", "?"),
            "capture": data.get("capture_device", "?"),
            "playback_label": data.get("playback_label", ""),
            "capture_label": data.get("capture_label", ""),
            "sample_rate": data.get("sample_rate"),
            "playback_format": data.get("playback_format", "?"),
            "capture_format": data.get("capture_format", "?"),
            "baseline_L_dbfs": analysis.get("L", {}).get("baseline_dbfs"),
            "baseline_R_dbfs": analysis.get("R", {}).get("baseline_dbfs"),
            "L_to_R_crosstalk_dbc": l_map.get("cross_channel_leakage_db"),
            "R_to_L_crosstalk_dbc": r_map.get("cross_channel_leakage_db"),
            "L_residual_median_dbfs": l_res.get("median"),
            "L_residual_p95_dbfs": l_res.get("p95"),
            "L_burst_windows": l_res.get("bursts"),
            "L_burst_episodes": l_res.get("episodes"),
            "R_residual_median_dbfs": r_res.get("median"),
            "R_residual_p95_dbfs": r_res.get("p95"),
            "R_burst_windows": r_res.get("bursts"),
            "R_burst_episodes": r_res.get("episodes"),
            "L_carrier_dropout_episodes": l_carrier.get("dropout_episodes"),
            "L_carrier_longest_dropout_ms": l_carrier.get("longest_dropout_ms"),
            "R_carrier_dropout_episodes": r_carrier.get("dropout_episodes"),
            "R_carrier_longest_dropout_ms": r_carrier.get("longest_dropout_ms"),
            "L_ultrasonic_40_nyquist_dbfs": l_ultra.get("median"),
            "R_ultrasonic_40_nyquist_dbfs": r_ultra.get("median"),
            "series_level_valid": data.get("series_level_valid"),
            "baseline_L_stable": analysis.get("L", {}).get("baseline_stable"),
            "baseline_R_stable": analysis.get("R", {}).get("baseline_stable"),
        }
        rows.append(row)
    headers = ("№", "ЦАП → АЦП", "Fs", "Шум тишины L/R", "Xtalk L→R / R→L",
               "Остаток L med/P95", "Остаток R med/P95", "Остаточные эпизоды L/R",
               "Carrier dropouts L/R", "40k–Nyq L/R")
    lines = ["СРАВНЕНИЕ ИЗМЕРЕНИЙ VALVEBENCH STEADY", "Разные пары устройств приведены как отдельные измерительные конфигурации.",
             " | ".join(headers), " | ".join(("---",) * len(headers))]
    for i, r in enumerate(rows, 1):
        def fmt(v, suffix=""):
            return "n/a" if v is None else f"{float(v):.1f}{suffix}"
        play_name = f"{r['playback_label']} [{r['playback']}]" if r["playback_label"] else r["playback"]
        capture_name = f"{r['capture_label']} [{r['capture']}]" if r["capture_label"] else r["capture"]
        pair = f"{play_name} → {capture_name}"
        lines.append(" | ".join((
            str(i), pair, str(r["sample_rate"]),
            f"{fmt(r['baseline_L_dbfs'])}/{fmt(r['baseline_R_dbfs'])}",
            f"{fmt(r['L_to_R_crosstalk_dbc'])}/{fmt(r['R_to_L_crosstalk_dbc'])}",
            f"{fmt(r['L_residual_median_dbfs'])}/{fmt(r['L_residual_p95_dbfs'])}",
            f"{fmt(r['R_residual_median_dbfs'])}/{fmt(r['R_residual_p95_dbfs'])}",
            f"{r['L_burst_episodes'] if r['L_burst_episodes'] is not None else 'n/a'}/"
            f"{r['R_burst_episodes'] if r['R_burst_episodes'] is not None else 'n/a'}",
            f"{(str(r['L_carrier_dropout_episodes']) + '/' + fmt(r['L_carrier_longest_dropout_ms'], 'ms')) if r['L_carrier_dropout_episodes'] is not None else 'n/a'}/"
            f"{(str(r['R_carrier_dropout_episodes']) + '/' + fmt(r['R_carrier_longest_dropout_ms'], 'ms')) if r['R_carrier_dropout_episodes'] is not None else 'n/a'}",
            f"{fmt(r['L_ultrasonic_40_nyquist_dbfs'])}/{fmt(r['R_ultrasonic_40_nyquist_dbfs'])}",
        )))
    return "\n".join(lines), rows


def _device_label(device: str) -> str:
    """Resolve a current ALSA card number to its current procfs label for the report."""
    match = re.match(r"^hw:(\d+)(?:,(\d+))?$", str(device))
    if not match:
        return str(device)
    card_no = int(match.group(1))
    try:
        from .backend import list_cards
        return next((name for number, name in list_cards() if number == card_no), f"ALSA card {card_no}")
    except Exception:
        return f"ALSA card {card_no}"


def _estimate_long_tone(y: np.ndarray, fs: int, nominal_hz: float, start_s: float, duration_s: float) -> tuple[float, float]:
    a = int(round(start_s * fs)); b = min(len(y), a + int(round(duration_s * fs)))
    if b - a < fs * 2:
        raise RuntimeError("Недостаточно данных для определения частоты тестового тона")
    return _tone_fit(y[a:b], fs, nominal_hz)


def run_steady_test(play_dev: str, rec_dev: str, fs: int = 192000, tone_hz: float = 997.0,
                    tone_level_dbfs: float = -18.0, target_dbfs: float = -23.4,
                    tolerance_db: float = 1.0, duration_s: float = 40.0,
                    window_s: float = 0.1, hop_s: float = 0.1, max_harmonics: int = 20,
                    idle_s: float = 15.0, gap_s: float = 8.0,
                    play_format: str = "auto", rec_format: str = "auto",
                    out_root: str | Path = "emu_measurements", input_fn=input,
                    print_fn=print) -> Path:
    """Interactive level match + sequential L/R steady-state measurement."""
    if fs < 48000 or fs > 384000:
        raise ValueError("Частота должна быть в диапазоне 48–384 кГц")
    if duration_s < 5 or duration_s > 300:
        raise ValueError("Длительность одного канала должна быть 5–300 секунд")
    if idle_s < 3 or idle_s > 120 or gap_s < 2 or gap_s > 60:
        raise ValueError("idle должен быть 3–120 с, gap — 2–60 с")
    if not (20.0 <= tone_hz <= min(20000.0, fs * 0.4)):
        raise ValueError("Частота тона вне допустимой полосы")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = Path(out_root) / f"steady-{stamp}"
    outdir.mkdir(parents=True, exist_ok=False)
    print_fn(f"Проверяю ALSA PCM: playback {play_dev}; capture {rec_dev}; {fs} Гц")
    play = choose_device_format(play_dev, fs, "playback", play_format)
    capture = choose_device_format(rec_dev, fs, "capture", rec_format)
    print_fn(f"Формат playback: {play.fmt}; формат capture: {capture.fmt}")
    print_fn(f"Цель: {target_dbfs:.2f} dBFS на каждом ADC; цифровой тон {tone_hz:g} Гц / {tone_level_dbfs:g} dBFS")
    print_fn("Подключение L→L, R→R. Для несбалансированных соединений используй соответствующие линейные входы; Phantom OFF.")
    trial = 1
    calibration_history = []
    while True:
        choice = input_fn("Enter — контроль уровня; q — выйти: ").strip().lower()
        if choice == "q":
            return outdir
        print_fn("  Записываю короткий контрольный тон…")
        l_db, r_db = _calibration_trial(outdir, trial, play, capture, fs, tone_hz, tone_level_dbfs)
        trial += 1
        dl, dr = l_db - target_dbfs, r_db - target_dbfs
        calibration_history.append({"trial": trial - 1, "adc_l_dbfs": l_db, "adc_r_dbfs": r_db,
                                    "delta_l_db": dl, "delta_r_db": dr})
        print_fn(f"  ADC L={l_db:.2f} dBFS ({dl:+.2f} дБ к цели) · ADC R={r_db:.2f} dBFS ({dr:+.2f} дБ к цели)")
        if abs(l_db - r_db) > 1.5:
            print_fn("  ПРЕДУПРЕЖДЕНИЕ: разница уровней L/R больше 1,5 дБ; общий регулятор не исправит этот дисбаланс.")
        if abs(dl) <= tolerance_db and abs(dr) <= tolerance_db:
            break
        avg = (dl + dr) / 2.0
        action = "УМЕНЬШИ" if avg > 0 else "УВЕЛИЧЬ"
        print_fn(f"  {action} выходной уровень/микшер примерно на {abs(avg):.1f} дБ и повтори.")
    choice = input_fn("Уровни согласованы. Не меняя громкость, Enter — основной тест; q — выход: ").strip().lower()
    if choice == "q":
        return outdir

    fade_s = 1.0
    play_seconds = 2.0 * duration_s + 2.0 * idle_s + gap_s + 4.0 * fade_s
    capture_seconds = play_seconds + 2.0
    play_file = outdir / "steady-playback.raw"
    capture_file = outdir / "steady-capture.raw"
    wav_file = outdir / "capture-float32.wav"
    csv_file = outdir / "timeline.csv"
    written_seconds = _write_test_program(play_file, play.fmt, fs, tone_hz, tone_level_dbfs,
                                          duration_s, idle_s, gap_s, fade_s)
    if abs(written_seconds - play_seconds) > 1.0 / fs:
        raise RuntimeError("Внутренняя ошибка расписания тестовой последовательности")
    print_fn(f"Основной тест: тишина {idle_s:.0f} с → L-only {duration_s:.0f} с → пауза {gap_s:.0f} с "
             f"→ R-only {duration_s:.0f} с → тишина {idle_s:.0f} с; запись около {capture_seconds:.0f} с.")
    lead_seconds = 1.0
    io_stderr = _capture_and_play(play_file, capture_file, play, capture, fs,
                                  capture_seconds, lead_seconds=lead_seconds)
    # 0.1.12-p1: XRUN-контроль по stderr обоих процессов
    io_health = {"xrun": io_health_mod.xrun_summary(
        Path(io_stderr["playback_stderr"]).read_text(errors="replace"),
        Path(io_stderr["capture_stderr"]).read_text(errors="replace"))}
    shutil.rmtree(io_stderr["err_dir"], ignore_errors=True)
    # 0.1.12-P0: capture декодируется потоково в float32 WAV, анализ читает окна
    # через memmap — пик памяти не зависит от длительности записи. Числа те же:
    # float32 точно хранит S24_3LE/S16_LE (мантисса 24 бита).
    frames = stream_to_float32_wav(capture_file, capture.fmt, capture.channels,
                                   wav_file, fs)
    raw = np.memmap(wav_file, dtype="<f4", mode="r", offset=FLOAT32_WAV_HEADER,
                    shape=(frames, capture.channels))
    # 0.1.12-p1: контроль фактической длины записи (жёсткий порог ниже остаётся)
    io_health["length"] = io_health_mod.length_status(
        frames, int(round(capture_seconds * fs)), fs)
    if raw.shape[0] < int(fs * (play_seconds + 1.0)):
        raise RuntimeError(f"Запись короче ожидаемой: {raw.shape[0] / fs:.2f} с")

    # Capture starts lead_seconds before playback. Exclude half-second margins
    # from idle states so USB/driver start/stop settling does not define baseline.
    # Capture-time ranges exclude fades from all idle/steady regions; active
    # ranges additionally receive a guard to absorb start-time uncertainty.
    ranges, transition_guard_s = _build_steady_state_ranges(
        lead_seconds, idle_s, duration_s, gap_s, fade_s, window_s)
    # 0.1.12-p1: фактическое начало воспроизведения по известной огибающей
    # (fade-in из _write_test_program), не порогом по амплитуде. Диапазоны
    # сдвигаются только при надёжном обнаружении и |сдвиг| > мёртвой зоны.
    t_onset_l = lead_seconds + idle_s
    t_onset_r = lead_seconds + idle_s + fade_s + duration_s + fade_s + gap_s
    onset = io_health_mod.detect_playback_onset(raw, fs, tone_hz, t_onset_l,
                                                t_onset_r, fade_s)
    io_health["onset"] = onset
    if onset["reliable"] and onset["applied"]:
        print_fn(f"  onset: измерено смещение запуска {onset['deviation_s']:+.3f} с; "
                 f"диапазоны состояний сдвинуты")
        ranges = {st: (a + onset["offset_s"], b + onset["offset_s"])
                  for st, (a, b) in ranges.items()}
    summaries = {}
    all_rows = []
    tone_levels = {}
    tone_map = {}
    f_by_state = {}
    # First estimate the active output frequency from its matching ADC channel.
    # Then project that same coherent frequency onto BOTH ADC channels so that
    # the report contains a real L/R channel map, not only the driven channel.
    for state, driven_idx, state_start in (("L_steady", 0, ranges["L_steady"][0]),
                                           ("R_steady", 1, ranges["R_steady"][0])):
        est_start = state_start + 0.5
        est_duration = min(3.0, duration_s - 1.0)
        f_est, _lev = _estimate_long_tone(raw[:, driven_idx], fs, tone_hz, est_start, est_duration)
        f_by_state[state] = f_est
        a = int(round(est_start * fs))
        b = min(len(raw), a + int(round(est_duration * fs)))
        map_levels = {
            "ADC_L": _tone_level_at(raw[a:b, 0], fs, f_est),
            "ADC_R": _tone_level_at(raw[a:b, 1], fs, f_est),
        }
        main_key = "ADC_L" if driven_idx == 0 else "ADC_R"
        leak_key = "ADC_R" if driven_idx == 0 else "ADC_L"
        main_level = map_levels[main_key]
        leak_level = map_levels[leak_key]
        measured_delta = main_level - target_dbfs
        within_target = abs(measured_delta) <= tolerance_db
        tone_map["PLAY_L_only" if state == "L_steady" else "PLAY_R_only"] = {
            "frequency_hz": f_est, "adc_levels_dbfs": map_levels,
            "cross_channel_leakage_db": leak_level - main_level,
            "main_level_delta_db": measured_delta, "main_level_within_target": within_target,
        }
        for idx, ch in ((0, "L"), (1, "R")):
            tone_levels[f"{state}_{ch}"] = {"frequency_hz": f_est, "level_dbfs": map_levels[f"ADC_{ch}"]}

    for idx, ch in ((0, "L"), (1, "R")):
        rows, stats = _analyse_channel(raw[:, idx], fs, ranges, ch, f_by_state, target_dbfs,
                                       window_s, hop_s, max_harmonics)
        all_rows.extend(rows)
        summaries[ch] = stats
    shared_stats = _shared_excursion_stats(all_rows, summaries, hop_s)
    tone_continuity = {}
    tone_continuity_rows = []
    for state, channel_idx, channel_name, map_key in (
        ("L_steady", 0, "L", "PLAY_L_only"),
        ("R_steady", 1, "R", "PLAY_R_only"),
    ):
        reference = tone_map[map_key]["adc_levels_dbfs"][f"ADC_{channel_name}"]
        continuity, continuity_rows = _tone_continuity_track(
            raw[:, channel_idx], fs, state, channel_name,
            ranges[state][0], ranges[state][1], f_by_state[state], reference,
            subwindow_ms=10.0, threshold_below_db=20.0, min_episode_ms=20.0,
        )
        tone_continuity[state] = continuity
        tone_continuity_rows.extend(continuity_rows)
    band_labels = [label for label, _lo, _hi in _ultrasonic_band_edges(fs)]
    csv_fields = ["time_s", "state", "channel", "residual_20_20k_dbfs", "raw_40_nyquist_dbfs",
                  "residual_removed_harmonics", "projection_frequency_hz"]
    for label in band_labels:
        csv_fields.extend((f"raw_{label}_dbfs", f"raw_{label}_asd_median_dbfs_sqrt_hz",
                           f"raw_{label}_asd_p95_dbfs_sqrt_hz"))
    with csv_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for row in all_rows:
            flat = {k: row.get(k) for k in csv_fields if k not in {f"raw_{b}_{s}" for b in band_labels for s in
                                                                    ("dbfs", "asd_median_dbfs_sqrt_hz", "asd_p95_dbfs_sqrt_hz")}}
            for label in band_labels:
                m = row.get("raw_ultrasonic_bands", {}).get(label)
                flat[f"raw_{label}_dbfs"] = m.get("dbfs") if m else None
                flat[f"raw_{label}_asd_median_dbfs_sqrt_hz"] = m.get("asd_median_dbfs_sqrt_hz") if m else None
                flat[f"raw_{label}_asd_p95_dbfs_sqrt_hz"] = m.get("asd_p95_dbfs_sqrt_hz") if m else None
            writer.writerow(flat)

    tone_csv_file = outdir / "tone-continuity.csv"
    tone_fields = ["start_s", "time_s", "state", "channel", "tone_level_dbfs",
                   "relative_to_reference_db", "below_threshold", "dropout_episode_id"]
    with tone_csv_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=tone_fields)
        writer.writeheader()
        writer.writerows(tone_continuity_rows)

    # Remove the temporary raw data and playback file; WAV/CSV retain the evidence.
    for tmp in (play_file, capture_file):
        try:
            tmp.unlink()
        except OSError:
            pass
    report = []
    report.append("VALVEBENCH STEADY-STATE DAC → ADC REPORT")
    report.append(f"Playback: {play_dev} ({play.fmt}); capture: {rec_dev} ({capture.fmt})")
    report.append(f"Fs={fs} Hz · tone={tone_hz:g} Hz at {tone_level_dbfs:g} dBFS · target ADC={target_dbfs:g}±{tolerance_db:g} dBFS")
    report.append(f"Window={window_s*1000:g} ms · hop={hop_s*1000:g} ms · LS removal DC + H1..H{max_harmonics}; tone leakage is not measured as noise")
    report.append(f"Steady analysis edge guard={transition_guard_s*1000:.0f} ms at each active-state boundary; playback duration is unchanged")
    report.append("MEASURED CHANNEL MAP / TONE:")
    for key, val in tone_map.items():
        levels = val["adc_levels_dbfs"]
        status = "LEVEL OK" if val["main_level_within_target"] else "LEVEL MISMATCH — серия невалидна для matched-level A/B"
        report.append(f"  {key}: f={val['frequency_hz']:.6f} Hz; ADC L={levels['ADC_L']:.2f} dBFS; "
                      f"ADC R={levels['ADC_R']:.2f} dBFS; cross-channel={val['cross_channel_leakage_db']:.1f} dB; "
                      f"main Δtarget={val['main_level_delta_db']:+.2f} dB [{status}]")
    # 0.1.12-p1: валидность серии = уровни ∧ нет XRUN ∧ длина в допуске ∧
    # надёжное выравнивание; причины невалидности перечисляются отдельно.
    validity_reasons = []
    for key, val in tone_map.items():
        if not val["main_level_within_target"]:
            validity_reasons.append(f"{key}: уровень {val['main_level_delta_db']:+.2f} dB от цели")
    xr = io_health["xrun"]
    if xr["total_xruns"]:
        validity_reasons.append(
            f"XRUN: playback underruns {xr['playback']['count']}, "
            f"capture overruns {xr['capture']['count']}")
    ln = io_health["length"]
    if ln["status"] != "ok":
        validity_reasons.append(f"длина записи {ln['status']}: "
                                f"{ln['deviation_ms']:+.1f} ms ({ln['deviation_frames']:+d} кадров)")
    if not io_health["onset"]["reliable"]:
        validity_reasons.append(f"выравнивание ненадёжно: {io_health['onset'].get('reason')}")
    series_valid = not validity_reasons
    report.append("SERIES LEVEL VALIDITY: " + ("OK" if series_valid else
                  "INVALID — " + "; ".join(validity_reasons)))
    report.append("INPUT/OUTPUT HEALTH (io_health):")
    report.append(f"  XRUN: playback underruns={xr['playback']['count']}, "
                  f"capture overruns={xr['capture']['count']} "
                  f"(stderr aplay {xr['playback']['stderr_bytes']} B, "
                  f"arecord {xr['capture']['stderr_bytes']} B); {xr['note']}")
    report.append(f"  length: {ln['status']} (отклонение {ln['deviation_ms']:+.1f} ms / "
                  f"{ln['deviation_frames']:+d} кадров от {ln['expected_frames']}; "
                  f"допуск ±{ln['tolerance_ms']:.0f} ms)")
    on = io_health["onset"]
    def fnum(v, spec):
        return "n/a" if v is None else format(v, spec)

    report.append(f"  onset: измерено {fnum(on['measured_onset_s'], '.3f')} s (ожидалось "
                  f"{on['expected_onset_s']:.3f} s, отклонение {fnum(on['deviation_s'], '+.3f')} s); "
                  f"SNR L {fnum(on['confidence']['snr_db_L'], '.0f')} / "
                  f"R {fnum(on['confidence']['snr_db_R'], '.0f')} dB; "
                  f"residual L {fnum(on['confidence']['residual_fraction_L'], '.3f')}; "
                  f"reliable={on['reliable']}; сдвиг применён={on['applied']}")
    if on["applied"]:
        report.append(f"  onset: диапазоны состояний сдвинуты на {on['offset_s']:+.3f} с")
    if validity_reasons:
        report.append("  ПРИЧИНЫ НЕВАЛИДНОСТИ: " + "; ".join(validity_reasons))
    report.append("IDLE BASELINE STABILITY (pre/post, dBFS; drift >3 dB is warning):")
    for ch in ("L", "R"):
        s = summaries[ch]
        post_text = f"{s['baseline_post_dbfs']:.2f}" if s["baseline_post_dbfs"] is not None else "n/a"
        drift_text = f"{s['baseline_drift_db']:+.2f} dB" if s["baseline_drift_db"] is not None else "n/a"
        stable_text = "stable" if s["baseline_stable"] is True else ("DRIFT" if s["baseline_stable"] is False else "not checked")
        report.append(f"  ADC {ch}: pre={s['baseline_pre_dbfs']:.2f}; post={post_text}; drift={drift_text} [{stable_text}]")
    report.append("IDLE ULTRASONIC BASELINE (integrated dBFS; ASD median dBFS/√Hz; pre → post):")
    for ch in ("L", "R"):
        for label, band in summaries[ch]["idle_ultrasonic_bands"].items():
            pre = band.get("idle_pre")
            post = band.get("idle_post")
            if pre is None and post is None:
                continue
            ptxt = f"{pre['dbfs_median']:.2f}/{pre['asd_median_dbfs_sqrt_hz']:.2f}" if pre else "n/a"
            qtxt = f"{post['dbfs_median']:.2f}/{post['asd_median_dbfs_sqrt_hz']:.2f}" if post else "n/a"
            report.append(f"  ADC {ch} {label}: {ptxt} → {qtxt}")
    report.append("ACTIVE CARRIER CONTINUITY (short-window tone fits; dropout = ≥20 ms at least 20 dB below measured reference):")
    for state in ("L_steady", "R_steady"):
        tc = tone_continuity[state]
        if not tc.get("available"):
            report.append(f"  {state}: unavailable — {tc.get('reason', 'unknown reason')}")
            continue
        episode_text = ", ".join(
            f"#{e['id']} {e['start_s']:.3f}–{e['end_s']:.3f}s ({e['duration_ms']:.0f}ms; min {e['min_tone_dbfs']:.1f} dBFS)"
            for e in tc["episodes"]
        ) or "нет"
        report.append(f"  {state} ADC {tc['channel']}: subwindow={tc['subwindow_ms']:.1f} ms; "
                      f"ref={tc['reference_dbfs']:.2f} dBFS; median={tc['median_tone_dbfs']:.2f}; "
                      f"p01={tc['p01_tone_dbfs']:.2f}; "
                      f"min={tc['min_tone_dbfs']:.2f}; below-threshold={tc['below_threshold_subwindows']}/"
                      f"{tc['subwindows']}; dropouts={tc['dropout_episodes']}; "
                      f"total={tc['total_dropout_ms']:.0f} ms; longest={tc['longest_dropout_ms']:.0f} ms")
        if tc["episodes"]:
            report.append("    events: " + episode_text)
    report.append("STEADY RESIDUAL EXCURSIONS: median / p95 / max dBFS; adjacent windows grouped into episodes; threshold=idle +6 dB")
    report.append("STATE       ADC    20–20k median / p95 / max              windows  episodes  longest-event  40k–Nyquist median / p95 / max")
    for ch in ("L", "R"):
        st = summaries[ch]
        report.append(f"IDLE BASELINE ADC {ch}: {st['baseline_dbfs']:.2f} dBFS")
        for state in ("L_steady", "R_steady"):
            q = st["states"].get(state)
            if not q:
                continue
            a = q["residual_20_20k"]; b = q["raw_40_nyquist"]
            high_band = (f"{b['median']:8.2f} / {b['p95']:8.2f} / {b['max']:8.2f}"
                         if b is not None else "       n/a /      n/a /      n/a")
            report.append(f"{state:10s} {ch:3s}  {a['median']:8.2f} / {a['p95']:8.2f} / {a['max']:8.2f}  "
                          f"{a['bursts']:4d}/{q['windows']:<4d}  {a['episodes']:4d}  {a['longest_episode_s']:6.2f} s  {high_band}")
            for label, band in q["raw_ultrasonic_bands"].items():
                if band is None:
                    continue
                report.append(f"    {label:7s}: level median/p95/max={band['dbfs_median']:.2f}/{band['dbfs_p95']:.2f}/{band['dbfs_max']:.2f} dBFS; "
                              f"ASD median/p95={band['asd_median_dbfs_sqrt_hz']:.2f}/{band['asd_p95_dbfs_sqrt_hz']:.2f} dBFS/√Hz")
    report.append("SIMULTANEOUS L/R EXCURSIONS (same windows, each channel's idle+6 dB threshold):")
    for state, st in shared_stats.items():
        corr = f"{st['correlation_db']:+.3f}" if st["correlation_db"] is not None else "n/a"
        report.append(f"  {state}: corr(residual dB)={corr}; L={st.get('L_excursion_windows', 0)}, "
                      f"R={st.get('R_excursion_windows', 0)}, simultaneous={st['both_excursion_windows']}, "
                      f"shared episodes={st['shared_episodes']} of {st['windows']} windows")
    report.append("TOP WINDOWS BY 20–20k RESIDUAL:")
    for ch in ("L", "R"):
        base = summaries[ch]["baseline_dbfs"]
        rows = [r for r in all_rows if r["channel"] == ch and r["state"] in ("L_steady", "R_steady")]
        rows.sort(key=lambda r: r["residual_20_20k_dbfs"], reverse=True)
        for r in rows[:8]:
            report.append(f"  ADC {ch} t={r['time_s']:.2f}s {r['state']:<9s} {r['residual_20_20k_dbfs']:.2f} dBFS "
                          f"({r['residual_20_20k_dbfs'] - base:+.2f} dB vs idle)")
    report.append("NOTES:")
    report.append("- Fundamental level is measured in the actual ADC recording; it is not inferred from the ALSA mixer percentage.")
    report.append("- Harmonics H1..Hn are removed by least-squares projection separately for each analysis window.")
    report.append("- 40k–Nyquist is a raw band level; no harmonic subtraction is applied there (H1..H20 are below 20 kHz at 997 Hz).")
    report.append("- Residual excursions and carrier dropouts are separate measurements: a missing carrier is not classified as additive noise.")
    report.append("- Carrier continuity is estimated on short contiguous windows; threshold events require the minimum consecutive duration. Times use the capture-recording time axis.")
    report.append("- Residual excursion windows are not independent physical events; adjacent windows are grouped into episodes.")
    report.append("- Idle baseline uses both long pre-test and post-test silence; their difference is reported separately.")
    report.append("- Ultrasonic bands report integrated dBFS and median/p95 spectral density in dBFS/√Hz.")
    report.append("- tone-continuity.csv contains the short-window active-carrier track; it is separate from the broadband residual timeline.")
    report.append("- This test localizes DAC/ADC paths; it does not identify a PCB component.")
    report.append("- Cross-channel tone levels are coherent projections at the measured source frequency, not broadband residual levels.")
    summary = "\n".join(report) + "\n"
    (outdir / "summary.txt").write_text(summary, encoding="utf-8")
    machine_report = {
        "schema": 1, "created": stamp, "playback_device": play_dev, "capture_device": rec_dev,
        "playback_label": _device_label(play_dev), "capture_label": _device_label(rec_dev),
        "playback_format": play.fmt, "capture_format": capture.fmt, "sample_rate": fs,
        "tone_hz_nominal": tone_hz, "tone_level_dbfs": tone_level_dbfs,
        "target_adc_dbfs": target_dbfs, "tolerance_db": tolerance_db,
        "calibration_trials": calibration_history, "series_level_valid": series_valid,
        "duration_per_channel_s": duration_s, "window_s": window_s, "hop_s": hop_s,
        "idle_seconds_each_end": idle_s, "gap_seconds": gap_s,
        "max_harmonics_removed": max_harmonics, "transition_guard_s": transition_guard_s,
        "measured_tones": tone_levels, "channel_map": tone_map,
        "io_health": io_health, "validity_reasons": validity_reasons,
        "tone_continuity": tone_continuity, "tone_continuity_csv": str(tone_csv_file),
        "analysis": summaries, "simultaneous_excursions": shared_stats,
    }
    (outdir / "steady.json").write_text(json.dumps(machine_report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_fn(summary)
    print_fn(f"Результаты сохранены: {outdir}")
    print_fn("Нужны: summary.txt, steady.json, timeline.csv, tone-continuity.csv и при событиях capture-float32.wav")
    return outdir
