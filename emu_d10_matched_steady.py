#!/usr/bin/env python3
"""Matched-level D10 -> E-MU ADC steady-state test.

Run from ValveBench project root:
    python3 emu_d10_matched_steady.py

First calibrates D10 analogue volume so a -18 dBFS 997 Hz tone gives
-23.4 dBFS at both E-MU ADC inputs (matching the internal E-MU loopback
measurement). Then records 40 s D10-L-only and 40 s D10-R-only.
The residual estimator removes DC + harmonics 1..20 by least-squares
projection in every window, avoiding the non-coherent FFT leakage bug in
the earlier steady test script.
"""
from __future__ import annotations

import csv
import math
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.optimize import minimize_scalar
from scipy.signal.windows import blackmanharris

FS = 192000
TONE_HZ = 997.0
TONE_DBFS = -18.0
TARGET_ADC_DBFS = -23.4
TARGET_TOL_DB = 1.0
DAC_DEVICE = "hw:3,0"  # SMSL D10
ADC_DEVICE = "hw:2,0"  # E-MU 0404
ADC_FORMAT = "S24_3LE"
DAC_FORMAT = "S32_LE"  # already confirmed working with this D10
LEAD = 1.0
PLAY_SECONDS = 95
CAPTURE_SECONDS = 97
FADE_SECONDS = 1.0
WINDOW_SECONDS = 0.100
HOP_SECONDS = 0.100
HARMONICS = 20

SEGMENTS = [
    ("idle_pre", 0.0, 3.0),
    ("L_fade_in", 3.0, 4.0),
    ("L_steady", 4.0, 44.0),
    ("L_fade_out", 44.0, 45.0),
    ("idle_gap", 45.0, 50.0),
    ("R_fade_in", 50.0, 51.0),
    ("R_steady", 51.0, 91.0),
    ("R_fade_out", 91.0, 92.0),
    ("idle_post", 92.0, 95.0),
]


def need_program(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"Не найдена команда {name!r}; установи пакет alsa-utils.")
    return p


def db10(x: float) -> float:
    return 10.0 * math.log10(max(float(x), 1e-300))


def dbfs_from_power(power: float) -> float:
    # Full-scale sine reference: 0 dBFS sine has peak 1 and RMS 1/sqrt(2).
    return 10.0 * math.log10(max(2.0 * float(power), 1e-300))


def pack_s32(x: np.ndarray) -> bytes:
    y = np.clip(np.rint(x * (2**31 - 1)), -(2**31), 2**31 - 1).astype("<i4")
    return y.tobytes()


def unpack_s24_3le(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    raw = raw[: raw.size - (raw.size % 6)]
    b = raw.reshape(-1, 2, 3).astype(np.int32)
    u = b[:, :, 0] | (b[:, :, 1] << 8) | (b[:, :, 2] << 16)
    signed = (u ^ 0x800000) - 0x800000
    return signed.astype(np.float64) / float(2**23)


def tone_file(seconds: float, mode: str) -> np.ndarray:
    n = int(round(seconds * FS))
    t = np.arange(n, dtype=np.float64) / FS
    amp = 10.0 ** (TONE_DBFS / 20.0)
    s = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
    x = np.zeros((n, 2), dtype=np.float64)
    if mode == "both":
        x[:, :] = s[:, None]
    elif mode == "L":
        x[:, 0] = s
    elif mode == "R":
        x[:, 1] = s
    else:
        raise ValueError(mode)
    return x


def write_s32(path: Path, x: np.ndarray) -> None:
    path.write_bytes(pack_s32(x))


def play(path: Path) -> None:
    cmd = [need_program("aplay"), "-q", "-D", DAC_DEVICE, "-t", "raw", "-f", DAC_FORMAT,
           "-r", str(FS), "-c", "2", str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("Ошибка воспроизведения D10: " + (p.stderr or p.stdout).strip())


def capture_with_playback(play_path: Path, capture_path: Path, capture_seconds: int) -> None:
    cmd = [need_program("arecord"), "-q", "-D", ADC_DEVICE, "-t", "raw", "-f", ADC_FORMAT,
           "-r", str(FS), "-c", "2", "-d", str(capture_seconds), str(capture_path)]
    rec = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(LEAD)
        play(play_path)
        _, err = rec.communicate(timeout=capture_seconds + 5)
        if rec.returncode:
            raise RuntimeError("Ошибка записи E-MU: " + (err or "").strip())
    except Exception:
        if rec.poll() is None:
            rec.terminate()
            try:
                rec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                rec.kill()
        raise


def fit_tone(y: np.ndarray, initial_hz: float = TONE_HZ) -> tuple[float, float]:
    """Estimate tone frequency and peak dBFS by least-squares sine/cosine fit."""
    y = np.asarray(y, dtype=np.float64)
    y = y - float(np.mean(y))
    n = len(y)
    t = np.arange(n, dtype=np.float64) / FS

    def coeff(f: float) -> tuple[float, float, float]:
        ph = 2.0 * np.pi * f * t
        s, c = np.sin(ph), np.cos(ph)
        a = 2.0 * float(np.dot(y, s)) / n
        b = 2.0 * float(np.dot(y, c)) / n
        explained = a * a + b * b
        return a, b, explained

    def objective(f: float) -> float:
        return -coeff(f)[2]

    opt = minimize_scalar(objective, bounds=(initial_hz - 0.5, initial_hz + 0.5),
                          method="bounded", options={"xatol": 1e-10, "maxiter": 100})
    f = float(opt.x)
    a, b, _ = coeff(f)
    peak = math.hypot(a, b)
    return f, 20.0 * math.log10(max(peak, 1e-30))


def make_q(n: int, f0: float) -> np.ndarray:
    """Orthonormal basis for DC and sine/cosine of harmonics 1..20."""
    t = np.arange(n, dtype=np.float64) / FS
    cols = [np.ones(n, dtype=np.float64)]
    for h in range(1, HARMONICS + 1):
        ph = 2.0 * np.pi * (h * f0) * t
        cols.append(np.sin(ph))
        cols.append(np.cos(ph))
    design = np.column_stack(cols)
    q, _ = np.linalg.qr(design, mode="reduced")
    return q


def window_psd(y: np.ndarray, win: np.ndarray, nfft: int, fs: int) -> tuple[np.ndarray, np.ndarray]:
    spec = np.fft.rfft(np.asarray(y, dtype=np.float64) * win, n=nfft)
    p = (np.abs(spec) ** 2) / (fs * np.sum(win ** 2))
    if nfft > 1:
        p[1:-1] *= 2.0
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    return f, p


def state_at(t: float) -> str:
    for name, start, end in SEGMENTS:
        if start <= t < end:
            return name
    return "pre_capture" if t < 0 else "post_playback"


def median(a: list[float]) -> float:
    return float(np.median(np.asarray(a, dtype=np.float64)))


def calibration_trial(outdir: Path, trial: int) -> tuple[float, float]:
    stimulus = outdir / f"calibration-{trial}.raw"
    capture = outdir / f"calibration-{trial}-capture.raw"
    write_s32(stimulus, tone_file(8.0, "both"))
    print("  Короткий контрольный тон на обоих каналах D10…")
    capture_with_playback(stimulus, capture, 10)
    x = unpack_s24_3le(capture)
    # Playback starts after LEAD; use a 5-second central region well inside tone.
    i0 = int((LEAD + 1.5) * FS)
    i1 = int((LEAD + 6.5) * FS)
    levels = []
    for ch in (0, 1):
        f, level = fit_tone(x[i0:i1, ch])
        levels.append(level)
    stimulus.unlink(missing_ok=True)
    capture.unlink(missing_ok=True)
    return levels[0], levels[1]


def make_main_stimulus(path: Path) -> None:
    x = np.zeros((PLAY_SECONDS * FS, 2), dtype=np.float64)
    amp = 10.0 ** (TONE_DBFS / 20.0)
    fade_n = int(round(FADE_SECONDS * FS))
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade_n, endpoint=True))
    for ch, start, end in ((0, 3.0, 45.0), (1, 50.0, 92.0)):
        n = int(round((end - start) * FS))
        t = np.arange(n, dtype=np.float64) / FS
        tone = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
        env = np.ones(n, dtype=np.float64)
        env[:fade_n] = ramp
        env[-fade_n:] = ramp[::-1]
        x[int(start * FS):int(end * FS), ch] = tone * env
    write_s32(path, x)


def main() -> int:
    need_program("arecord")
    need_program("aplay")
    root = Path.cwd()
    outdir = root / "emu_measurements" / f"D10-matched-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    print("E-MU 0404 / SMSL D10 · MATCHED-LEVEL STEADY TEST")
    print(f"D10={DAC_DEVICE} → E-MU ADC={ADC_DEVICE} · Fs={FS} · digital tone={TONE_DBFS:.1f} dBFS")
    print(f"Цель: {TARGET_ADC_DBFS:.1f} dBFS на каждом входе E-MU (допуск ±{TARGET_TOL_DB:.1f} dB).")
    print("Подключение: D10 3.5 мм L/R → E-MU L/R; оба входа Grounded Input; Phantom OFF.")
    print("Не использовать D10 4.4 мм balanced. Не менять входные настройки E-MU.")
    input("Подключи D10 и нажми Enter. Сначала будет короткая настройка уровня: ")

    trial = 1
    while True:
        l, r = calibration_trial(outdir, trial)
        dl, dr = TARGET_ADC_DBFS - l, TARGET_ADC_DBFS - r
        print(f"  ADC L={l:.2f} dBFS ({dl:+.2f} dB до цели) · ADC R={r:.2f} dBFS ({dr:+.2f} dB до цели)")
        if abs(dl) <= TARGET_TOL_DB and abs(dr) <= TARGET_TOL_DB:
            ans = input("Уровни совпали с целью. Не трогая регулятор D10, нажми Enter для основного теста (q — выход): ").strip().lower()
            if ans == "q":
                return 0
            break
        direction = "УВЕЛИЧЬ" if (dl + dr) / 2.0 > 0 else "УМЕНЬШИ" 
        print(f"  {direction} громкость D10 примерно на {(dl + dr) / 2.0:+.1f} dB, затем повтори контроль.")
        ans = input("Enter — повторить контроль, q — выйти: ").strip().lower()
        if ans == "q":
            print("Остановлено; основной тест не запускался.")
            return 0
        trial += 1
        if trial > 12:
            raise RuntimeError("Слишком много попыток настройки уровня; проверь D10/кабели/уровни.")

    stimulus = outdir / "d10-stimulus-s32le.raw"
    capture = outdir / "emu-capture-s24_3le.raw"
    make_main_stimulus(stimulus)
    print("Основной тест: 40 с L-only + 40 с R-only, плавные переходы; запись около 97 с.")
    capture_with_playback(stimulus, capture, CAPTURE_SECONDS)
    x = unpack_s24_3le(capture)
    if len(x) < FS * 95:
        raise RuntimeError(f"Запись короткая: {len(x)/FS:.2f} s, ожидалось ≥95 s")
    wavfile.write(outdir / "emu-capture-float32.wav", FS, x.astype(np.float32))
    # Keep the portable WAV and reports; remove large temporary raw files.
    capture.unlink(missing_ok=True)
    stimulus.unlink(missing_ok=True)

    # Frequency estimate in the middle 8 s of each driven channel.
    fits = {}
    for state, centre, ch in (("L_steady", 24.0, 0), ("R_steady", 71.0, 1)):
        c0 = int((LEAD + centre - 4.0) * FS)
        c1 = int((LEAD + centre + 4.0) * FS)
        f, level = fit_tone(x[c0:c1, ch])
        fits[state] = (f, level)

    nwin = int(round(WINDOW_SECONDS * FS))
    hop = int(round(HOP_SECONDS * FS))
    nfft = nwin * 2
    win = blackmanharris(nwin, sym=False)
    fgrid = np.fft.rfftfreq(nfft, 1.0 / FS)
    band_audio = (fgrid >= 20.0) & (fgrid < 20000.0)
    band_ultra = (fgrid >= 40000.0) & (fgrid < 96000.0)
    q_by_state = {state: make_q(nwin, fit[0]) for state, fit in fits.items()}
    rows = []
    for start in range(0, len(x) - nwin + 1, hop):
        cap_centre = (start + nwin / 2) / FS
        pb_centre = cap_centre - LEAD
        state = state_at(pb_centre)
        row = {"capture_time_s": cap_centre, "playback_time_s": pb_centre, "state": state}
        for ch, name in ((0, "L"), (1, "R")):
            frame = x[start:start+nwin, ch]
            f, p_raw = window_psd(frame, win, nfft, FS)
            p_ultra = dbfs_from_power(float(np.sum(p_raw[band_ultra]) * (FS / nfft)))
            if state in q_by_state:
                q = q_by_state[state]
                residual = frame - q @ (q.T @ frame)
                _, p_res = window_psd(residual, win, nfft, FS)
                p_audio = dbfs_from_power(float(np.sum(p_res[band_audio]) * (FS / nfft)))
            else:
                p_audio = dbfs_from_power(float(np.sum(p_raw[band_audio]) * (FS / nfft)))
            row[f"{name}_20-20k_residual_dbfs"] = p_audio
            row[f"{name}_40-96k_dbfs"] = p_ultra
        rows.append(row)

    idle_states = {"idle_pre", "idle_gap", "idle_post"}
    baseline = {}
    for ch in ("L", "R"):
        baseline[ch] = median([float(r[f"{ch}_20-20k_residual_dbfs"]) for r in rows if r["state"] in idle_states])

    def stats(ch: str, state: str, key: str, base: float) -> tuple[float, float, float, int, int]:
        vals = [float(r[key]) for r in rows if r["state"] == state]
        if not vals:
            return float("nan"), float("nan"), float("nan"), 0, 0
        return median(vals), float(np.percentile(vals, 95)), max(vals), sum(v > base + 6.0 for v in vals), len(vals)

    report = [
        "E-MU 0404 / SMSL D10 · MATCHED-LEVEL STEADY-STATE REPORT",
        f"Fs={FS} · ADC={ADC_FORMAT} · D10={DAC_FORMAT} · tone={TONE_HZ:.3f} Hz at {TONE_DBFS:.1f} dBFS",
        f"Target ADC level={TARGET_ADC_DBFS:.1f}±{TARGET_TOL_DB:.1f} dBFS; window={WINDOW_SECONDS*1000:.0f} ms; hop={HOP_SECONDS*1000:.0f} ms",
        "Residual: per-window least-squares removal of DC and harmonics 1..20; no fixed-bin notches.",
        "40–96 kHz is raw-band level; audio residual values are measured after harmonic projection.",
        "",
        "MEASURED TONE / CHANNEL MAP:",
        f"  D10_L_only: ADC L={fits['L_steady'][1]:.2f} dBFS at {fits['L_steady'][0]:.6f} Hz",
        f"  D10_R_only: ADC R={fits['R_steady'][1]:.2f} dBFS at {fits['R_steady'][0]:.6f} Hz",
        f"  f0 difference={abs(fits['L_steady'][0]-fits['R_steady'][0]):.6f} Hz",
        f"IDLE BASELINE: L={baseline['L']:.2f} dBFS · R={baseline['R']:.2f} dBFS",
        "STATE       ADC    20-20k median / p95 / max dBFS       >baseline+6dB       40-96k median / p95 / max dBFS",
    ]
    for state in ("L_steady", "R_steady"):
        for ch in ("L", "R"):
            med, p95, mx, count, total = stats(ch, state, f"{ch}_20-20k_residual_dbfs", baseline[ch])
            u = stats(ch, state, f"{ch}_40-96k_dbfs", median([float(r[f"{ch}_40-96k_dbfs"]) for r in rows if r["state"] in idle_states]))
            report.append(f"{state:9s} {ch:>2s}  {med:8.2f} / {p95:8.2f} / {mx:8.2f}   {count:4d}/{total:<4d}   {u[0]:8.2f} / {u[1]:8.2f} / {u[2]:8.2f}")
    report += ["", "TOP WINDOWS DURING STEADY TONE:"]
    candidates = []
    for ch in ("L", "R"):
        key = f"{ch}_20-20k_residual_dbfs"
        for r in rows:
            if r["state"] in ("L_steady", "R_steady"):
                candidates.append((float(r[key]), ch, r))
    for level, ch, r in sorted(candidates, reverse=True)[:20]:
        report.append(f"  ADC {ch} t={float(r['playback_time_s']):6.2f}s {r['state']:8s} {level:8.2f} dBFS ({level-baseline[ch]:+.2f} dB vs idle)")
    report += ["", "Interpretation: compare this report with the E-MU internal-loopback corrected analysis at the same ADC tone level.",
               "If left-channel excursions persist with D10 at matched level, suspect ADC-L/input-local coupling.",
               "If D10 is clean at matched level but E-MU internal DAC loopback is not, suspicion shifts to DAC-L/output path or its interaction with the input.",
               "This test alone cannot identify a specific PCB component."]

    (outdir / "summary.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    with (outdir / "burst-timeline.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("\n".join(report))
    print(f"\nРезультаты: {outdir}")
    print("Нужные файлы: summary.txt, burst-timeline.csv, emu-capture-float32.wav")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ОШИБКА: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
