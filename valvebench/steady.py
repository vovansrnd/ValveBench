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
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import fft as sp_fft
from scipy.io import wavfile
from scipy.optimize import minimize_scalar
from scipy.signal.windows import blackmanharris, hann

from .pcm import FORMATS, to_pcm, from_pcm


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
                      lead_seconds: float = 0.5) -> None:
    rec_cmd = ["arecord", "-q", "-D", capture.device, "-t", "raw", "-f", capture.fmt,
               "-r", str(fs), "-c", str(capture.channels), "-d",
               str(max(1, int(math.ceil(capture_seconds))),), str(capture_file)]
    rec = subprocess.Popen(rec_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(lead_seconds)
        play_cmd = ["aplay", "-q", "-D", play.device, "-t", "raw", "-f", play.fmt,
                    "-r", str(fs), "-c", str(play.channels), str(play_file)]
        p = subprocess.run(play_cmd, capture_output=True, text=True,
                           timeout=capture_seconds + 10.0)
        if p.returncode:
            raise RuntimeError(f"Ошибка воспроизведения {play.device} ({play.fmt}): "
                               + (p.stderr or p.stdout).strip())
        _, err = rec.communicate(timeout=capture_seconds + 10.0)
        if rec.returncode:
            raise RuntimeError(f"Ошибка записи {capture.device} ({capture.fmt}): {(err or '').strip()}")
    except Exception:
        if rec.poll() is None:
            rec.terminate()
            try:
                rec.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                rec.kill()
                rec.wait(timeout=2.0)
        raise


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
                        play_seconds: float = 95.0) -> None:
    """Write the timed L-only/R-only test in one-second chunks; phase is continuous."""
    amp = 10.0 ** (level_dbfs / 20.0)
    total = int(round(play_seconds * fs))
    chunk = fs  # one second per write
    with path.open("wb") as fh:
        for start in range(0, total, chunk):
            count = min(chunk, total - start)
            local = np.arange(start, start + count, dtype=np.float64) / fs
            # 0–3 idle, 3–4 L fade in, 4–44 L steady, 44–45 fade out,
            # 45–50 gap, 50–51 R fade in, 51–91 R steady, 91–92 fade out.
            left = np.zeros(count, dtype=np.float64)
            right = np.zeros(count, dtype=np.float64)
            # Total layout is 3 s idle, 1 s fade-in, L steady, 1 s fade-out,
            # 5 s gap, 1 s fade-in, R steady, 1 s fade-out, 3 s idle.
            # duration is recovered from the known total length: play_seconds = 15 + 2*duration.
            duration = (play_seconds - 15.0) / 2.0
            l_stop = 4.0 + duration
            l_end = l_stop + 1.0
            r_fade_start = l_end + 5.0
            r_steady_start = r_fade_start + 1.0
            r_stop = r_steady_start + duration
            r_end = r_stop + 1.0
            idx_l = (local >= 3.0) & (local < l_end)
            idx_r = (local >= r_fade_start) & (local < r_end)
            if np.any(idx_l):
                env = np.ones(np.count_nonzero(idx_l), dtype=np.float64)
                tt = local[idx_l]
                fi = (tt < 4.0)
                fo = (tt >= l_stop)
                env[fi] = tt[fi] - 3.0
                env[fo] = l_end - tt[fo]
                left[idx_l] = amp * env * np.sin(2.0 * np.pi * f * local[idx_l])
            if np.any(idx_r):
                env = np.ones(np.count_nonzero(idx_r), dtype=np.float64)
                tt = local[idx_r]
                fi = (tt < r_steady_start)
                fo = (tt >= r_stop)
                env[fi] = tt[fi] - r_fade_start
                env[fo] = r_end - tt[fo]
                right[idx_r] = amp * env * np.sin(2.0 * np.pi * f * local[idx_r])
            data = np.column_stack((left, right))
            fh.write(to_pcm(data, fmt))


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


def _band_dbfs(x: np.ndarray, fs: int, lo: float, hi: float, window: np.ndarray) -> float:
    n = len(x)
    spec = np.fft.rfft(np.asarray(x, dtype=np.float64) * window)
    power = np.abs(spec) ** 2 / (fs * float(np.sum(window ** 2)))
    if n > 1:
        power[1:-1] *= 2.0
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    mask = (freqs >= lo) & (freqs <= min(hi, fs / 2.0))
    df = fs / n
    variance = float(np.sum(power[mask]) * df)
    return 10.0 * math.log10(max(2.0 * variance, 1e-300))


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
    baseline_values = []
    for state, (start_s, end_s) in state_ranges.items():
        start = int(round(start_s * fs)); end = min(len(y), int(round(end_s * fs)))
        for off in range(start, max(start, end - n + 1), hop):
            raw = np.asarray(y[off:off + n], dtype=np.float64)
            if raw.size != n:
                continue
            t_mid = (off + n / 2.0) / fs
            if state.startswith("idle"):
                residual = raw - float(np.mean(raw))
            else:
                f_state = float(f0.get(state, f0.get("default"))) if isinstance(f0, dict) else float(f0)
                if f_state not in q_cache:
                    q_cache[f_state] = _make_projection(n, fs, f_state, max_harmonics)
                q = q_cache[f_state]
                coeff = q.T @ raw
                residual = raw - q @ coeff
            lev20 = _band_dbfs(residual, fs, 20.0, 20000.0, win)
            # Below 80 kHz sample rate the requested ultrasonic band does not exist.
            # Do not print an artificial -300 dBFS value as if it were a measurement.
            lev40 = (_band_dbfs(raw, fs, 40000.0, fs / 2.0, win)
                     if fs / 2.0 > 40000.0 else None)
            row = {"time_s": t_mid, "state": state, "channel": channel,
                   "residual_20_20k_dbfs": lev20, "raw_40_nyquist_dbfs": lev40,
                   "residual_removed_harmonics": 0 if state.startswith("idle") else int((q.shape[1] - 1) // 2),
                   "projection_frequency_hz": None if state.startswith("idle") else f_state}
            out.append(row)
            if state == "idle_pre":
                baseline_values.append(lev20)
    base = float(np.median(baseline_values)) if baseline_values else float("nan")
    summary = {"baseline_dbfs": base, "states": {}}
    for state in ("L_steady", "R_steady"):
        vals = [r for r in out if r["state"] == state]
        v20 = np.asarray([r["residual_20_20k_dbfs"] for r in vals], dtype=float)
        v40_values = [r["raw_40_nyquist_dbfs"] for r in vals if r["raw_40_nyquist_dbfs"] is not None]
        v40 = np.asarray(v40_values, dtype=float)
        if not v20.size:
            continue
        summary["states"][state] = {
            "windows": int(v20.size),
            "residual_20_20k": {"median": float(np.median(v20)), "p95": float(np.percentile(v20, 95)),
                                "max": float(np.max(v20)), "bursts": int(np.count_nonzero(v20 > base + 6.0)),
                                "threshold_dbfs": base + 6.0},
            "raw_40_nyquist": ({"median": float(np.median(v40)), "p95": float(np.percentile(v40, 95)),
                                "max": float(np.max(v40))} if v40.size else None),
        }
    return out, summary


def _estimate_long_tone(y: np.ndarray, fs: int, nominal_hz: float, start_s: float, duration_s: float) -> tuple[float, float]:
    a = int(round(start_s * fs)); b = min(len(y), a + int(round(duration_s * fs)))
    if b - a < fs * 2:
        raise RuntimeError("Недостаточно данных для определения частоты тестового тона")
    return _tone_fit(y[a:b], fs, nominal_hz)


def run_steady_test(play_dev: str, rec_dev: str, fs: int = 192000, tone_hz: float = 997.0,
                    tone_level_dbfs: float = -18.0, target_dbfs: float = -23.4,
                    tolerance_db: float = 1.0, duration_s: float = 40.0,
                    window_s: float = 0.1, hop_s: float = 0.1, max_harmonics: int = 20,
                    play_format: str = "auto", rec_format: str = "auto",
                    out_root: str | Path = "emu_measurements", input_fn=input,
                    print_fn=print) -> Path:
    """Interactive level match + sequential L/R steady-state measurement."""
    if fs < 48000 or fs > 384000:
        raise ValueError("Частота должна быть в диапазоне 48–384 кГц")
    if duration_s < 5 or duration_s > 300:
        raise ValueError("Длительность одного канала должна быть 5–300 секунд")
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

    play_seconds = 2.0 * duration_s + 15.0
    capture_seconds = play_seconds + 2.0
    play_file = outdir / "steady-playback.raw"
    capture_file = outdir / "steady-capture.raw"
    wav_file = outdir / "capture-float32.wav"
    csv_file = outdir / "timeline.csv"
    _write_test_program(play_file, play.fmt, fs, tone_hz, tone_level_dbfs, play_seconds)
    print_fn(f"Основной тест: L-only {duration_s:.0f} с + R-only {duration_s:.0f} с; запись около {capture_seconds:.0f} с.")
    _capture_and_play(play_file, capture_file, play, capture, fs, capture_seconds, lead_seconds=1.0)
    raw = from_pcm(capture_file.read_bytes(), capture.fmt, capture.channels)
    if raw.shape[0] < int(fs * (play_seconds + 1.0)):
        raise RuntimeError(f"Запись короче ожидаемой: {raw.shape[0] / fs:.2f} с")
    wavfile.write(wav_file, fs, raw.astype(np.float32))

    # The playback schedule has 3 s idle, 1 s fade, duration_s steady, 1 s fade,
    # 5 s gap, 1 s fade, duration_s steady, 1 s fade, then 3 s idle.
    l_start, l_end = 5.0, 5.0 + duration_s
    r_start, r_end = 12.0 + duration_s, 12.0 + 2.0 * duration_s
    # Capture starts one second before playback; these ranges are capture-relative.
    ranges = {"idle_pre": (1.0, 3.5), "L_steady": (l_start, l_end),
              "idle_gap": (6.0 + duration_s, 9.0 + duration_s),
              "R_steady": (r_start, r_end),
              "idle_post": (13.0 + 2.0 * duration_s, 16.0 + 2.0 * duration_s)}
    summaries = {}
    all_rows = []
    tone_levels = {}
    tone_map = {}
    f_by_state = {}
    # First estimate the active output frequency from its matching ADC channel.
    # Then project that same coherent frequency onto BOTH ADC channels so that
    # the report contains a real L/R channel map, not only the driven channel.
    for state, driven_idx, state_start in (("L_steady", 0, l_start), ("R_steady", 1, r_start)):
        est_start = state_start + 1.0
        f_est, _lev = _estimate_long_tone(raw[:, driven_idx], fs, tone_hz, est_start, min(4.0, duration_s - 1.0))
        f_by_state[state] = f_est
        a = int(round(est_start * fs))
        b = min(len(raw), a + int(round(min(4.0, duration_s - 1.0) * fs)))
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
    with csv_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["time_s", "state", "channel", "residual_20_20k_dbfs",
                                                "raw_40_nyquist_dbfs", "residual_removed_harmonics",
                                                "projection_frequency_hz"])
        writer.writeheader(); writer.writerows(all_rows)

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
    report.append("MEASURED CHANNEL MAP / TONE:")
    for key, val in tone_map.items():
        levels = val["adc_levels_dbfs"]
        status = "LEVEL OK" if val["main_level_within_target"] else "LEVEL MISMATCH — серия невалидна для matched-level A/B"
        report.append(f"  {key}: f={val['frequency_hz']:.6f} Hz; ADC L={levels['ADC_L']:.2f} dBFS; "
                      f"ADC R={levels['ADC_R']:.2f} dBFS; cross-channel={val['cross_channel_leakage_db']:.1f} dB; "
                      f"main Δtarget={val['main_level_delta_db']:+.2f} dB [{status}]")
    series_valid = all(v["main_level_within_target"] for v in tone_map.values())
    report.append("SERIES LEVEL VALIDITY: " + ("OK" if series_valid else "INVALID — measured main tone differs from target"))
    report.append("STEADY RESIDUALS: median / p95 / max dBFS; bursts > idle +6 dB")
    report.append("STATE       ADC    20–20k median / p95 / max             bursts        40k–Nyquist median / p95 / max")
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
                          f"{a['bursts']:4d}/{q['windows']:<4d}  {high_band}")
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
    report.append("- A burst count is a threshold statistic over overlapping or adjacent windows, not a count of independent physical events.")
    report.append("- This test localizes DAC/ADC paths; it does not identify a PCB component.")
    report.append("- Cross-channel tone levels are coherent projections at the measured source frequency, not broadband residual levels.")
    summary = "\n".join(report) + "\n"
    (outdir / "summary.txt").write_text(summary, encoding="utf-8")
    machine_report = {
        "schema": 1, "created": stamp, "playback_device": play_dev, "capture_device": rec_dev,
        "playback_format": play.fmt, "capture_format": capture.fmt, "sample_rate": fs,
        "tone_hz_nominal": tone_hz, "tone_level_dbfs": tone_level_dbfs,
        "target_adc_dbfs": target_dbfs, "tolerance_db": tolerance_db,
        "calibration_trials": calibration_history, "series_level_valid": series_valid,
        "duration_per_channel_s": duration_s, "window_s": window_s, "hop_s": hop_s,
        "max_harmonics_removed": max_harmonics, "measured_tones": tone_levels,
        "channel_map": tone_map, "analysis": summaries,
    }
    (outdir / "steady.json").write_text(json.dumps(machine_report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_fn(summary)
    print_fn(f"Результаты сохранены: {outdir}")
    print_fn("Нужны: summary.txt, steady.json, timeline.csv и при всплесках capture-float32.wav")
    return outdir
