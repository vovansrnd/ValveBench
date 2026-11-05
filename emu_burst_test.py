#!/usr/bin/env python3
"""Transient-noise localisation test: SMSL D10 playback -> E-MU 0404 ADC.

Run from ValveBench project directory:
    python3 emu_burst_test.py

Uses D10 hw:3,0 for playback and E-MU hw:2,0 for capture at 192 kHz.
Records both ADC channels while D10 plays: silence, L-only tone, silence,
R-only tone, silence. Reports short-time 20 Hz-20 kHz and 40-96 kHz noise,
including median / 95th percentile / maximum and timestamps of peaks.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal.windows import blackmanharris

FS = 192000
TONE_HZ = 997.0
TONE_DBFS = -18.0
DAC_DEVICE = "hw:3,0"   # SMSL D10 from the user's aplay -l
ADC_DEVICE = "hw:2,0"   # E-MU 0404
ADC_FORMAT = "S24_3LE"
CAPTURE_SECONDS = 35
PLAYBACK_LEAD_SECONDS = 1.0
SEGMENTS = [
    ("idle_pre", 0.0, 3.0),
    ("D10_L_only", 3.0, 13.0),
    ("gap", 13.0, 17.0),
    ("D10_R_only", 17.0, 27.0),
    ("idle_post", 27.0, 31.0),
]
WINDOW_SECONDS = 0.25
HOP_SECONDS = 0.125


def need_program(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"Не найдена команда {name!r} (пакет alsa-utils).")
    return p


def pack_s32(x: np.ndarray) -> bytes:
    y = np.clip(np.rint(x * (2**31 - 1)), -(2**31), 2**31 - 1).astype("<i4")
    return y.tobytes()


def build_playback() -> np.ndarray:
    total = int(round(31.0 * FS))
    x = np.zeros((total, 2), dtype=np.float64)
    amp = 10.0 ** (TONE_DBFS / 20.0)
    for name, start, end in SEGMENTS:
        if name not in ("D10_L_only", "D10_R_only"):
            continue
        i0, i1 = int(start * FS), int(end * FS)
        t = np.arange(i1 - i0, dtype=np.float64) / FS
        tone = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
        ch = 0 if name == "D10_L_only" else 1
        x[i0:i1, ch] = tone
    return x


def unpack_s24_3le(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    frame_bytes = 2 * 3
    if raw.size % frame_bytes:
        raw = raw[: raw.size - (raw.size % frame_bytes)]
    b = raw.reshape(-1, 2, 3).astype(np.int32)
    u = b[:, :, 0] | (b[:, :, 1] << 8) | (b[:, :, 2] << 16)
    signed = (u ^ 0x800000) - 0x800000
    return signed.astype(np.float64) / float(2**23)


def probe_d10(fmt: str, probe_file: Path) -> tuple[bool, str]:
    cmd = [need_program("aplay"), "-q", "-D", DAC_DEVICE, "-t", "raw", "-f", fmt,
           "-r", str(FS), "-c", "2", str(probe_file)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return False, "проба воспроизведения зависла"
    if p.returncode == 0:
        return True, "OK"
    return False, (p.stderr or p.stdout or f"код {p.returncode}").strip()


def dbfs_from_power(power: float) -> float:
    # Power is mean-square relative to digital peak=1; report full-scale-sine reference.
    return 10.0 * math.log10(max(2.0 * float(power), 1e-300))


def frame_spectrum(y: np.ndarray, fs: int, window: np.ndarray, nfft: int,
                   freqs: np.ndarray, band: tuple[float, float]) -> float:
    y = np.asarray(y, dtype=np.float64)
    y = y - np.mean(y)
    yw = y * window
    spec = np.fft.rfft(yw, n=nfft)
    psd = (np.abs(spec) ** 2) / (fs * np.sum(window ** 2))
    if nfft > 1:
        psd[1:-1] *= 2.0
    df = fs / nfft
    lo, hi = band
    m = (freqs >= lo) & (freqs < hi)
    # Exclude the fundamental and its harmonics: we want broadband/non-harmonic bursts.
    if lo <= TONE_HZ <= hi or hi > 20000:
        harmonics = np.arange(1, int(hi // TONE_HZ) + 1, dtype=float) * TONE_HZ
        for h in harmonics:
            if lo <= h < hi:
                m &= np.abs(freqs - h) > 18.0
    power = float(np.sum(psd[m]) * df)
    return dbfs_from_power(power)


def tone_level(y: np.ndarray, fs: int) -> float:
    # Least-squares sine/cosine projection at 997 Hz; robust to non-coherent capture start.
    y = np.asarray(y, dtype=np.float64)
    n = len(y)
    t = np.arange(n, dtype=np.float64) / fs
    s = np.sin(2.0 * np.pi * TONE_HZ * t)
    c = np.cos(2.0 * np.pi * TONE_HZ * t)
    a = 2.0 * np.dot(y, s) / n
    b = 2.0 * np.dot(y, c) / n
    peak = math.hypot(float(a), float(b))
    return 20.0 * math.log10(max(peak, 1e-30))


def summarise(values: list[float]) -> tuple[float, float, float]:
    a = np.asarray(values, dtype=float)
    return float(np.median(a)), float(np.percentile(a, 95)), float(np.max(a))


def main() -> int:
    need_program("arecord")
    need_program("aplay")
    root = Path.cwd()
    outdir = root / "emu_measurements" / f"D10-burst-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    print("E-MU ADC transient test using external SMSL D10")
    print(f"DAC: {DAC_DEVICE} · ADC: {ADC_DEVICE} · Fs={FS} · tone={TONE_HZ:.0f} Hz / {TONE_DBFS:.1f} dBFS")
    print("Физическое подключение:")
    print("  D10: 3.5-mm stereo headphone output -> 2xRCA breakout")
    print("  D10 Left/white -> E-MU ADC L; D10 Right/red -> E-MU ADC R")
    print("  Оба E-MU входа в Grounded Input; phantom power OFF.")
    print("  Не подключать 4.4-mm balanced output. На D10 установить постоянную громкость.")
    print("Сначала D10 выдаст L-only тон, затем R-only тон; оба входа E-MU записываются постоянно.")
    input("Подключи кабели и нажми Enter для начала; Ctrl+C отменяет: ")

    # Short silent probe verifies D10's actual ALSA format/rate without sending a signal.
    probe = outdir / "d10-format-probe.raw"
    probe.write_bytes(bytes(2 * 4 * int(0.1 * FS)))
    fmt = None
    errors = []
    for candidate in ("S32_LE", "S24_3LE", "S24_LE"):
        ok, msg = probe_d10(candidate, probe)
        if ok:
            fmt = candidate
            break
        errors.append(f"{candidate}: {msg}")
    probe.unlink(missing_ok=True)
    if fmt is None:
        raise RuntimeError("D10 не открылся на 192 кГц ни в одном формате:\n" + "\n".join(errors))
    print(f"D10 открыт на 192 кГц, формат {fmt}.")

    playback = build_playback()
    if fmt == "S32_LE":
        playback_path = outdir / "d10-stimulus.raw"
        playback_path.write_bytes(pack_s32(playback))
    elif fmt == "S24_3LE":
        p = np.clip(np.rint(playback * (2**23 - 1)), -(2**23), 2**23 - 1).astype(np.int32)
        u = p & 0xFFFFFF
        packed = np.empty((len(p), 2, 3), dtype=np.uint8)
        packed[:, :, 0] = u & 0xFF
        packed[:, :, 1] = (u >> 8) & 0xFF
        packed[:, :, 2] = (u >> 16) & 0xFF
        playback_path = outdir / "d10-stimulus.raw"
        packed.tofile(playback_path)
    else:  # S24_LE: 24 significant bits in a 32-bit container, left-justified.
        p = np.clip(np.rint(playback * (2**23 - 1)), -(2**23), 2**23 - 1).astype(np.int32)
        playback_path = outdir / "d10-stimulus.raw"
        (p << 8).astype("<i4").tofile(playback_path)

    capture_path = outdir / "emu-capture-s24_3le.raw"
    arecord_cmd = [need_program("arecord"), "-q", "-D", ADC_DEVICE, "-t", "raw", "-f", ADC_FORMAT,
                   "-r", str(FS), "-c", "2", "-d", str(CAPTURE_SECONDS), str(capture_path)]
    print("Начинаю запись E-MU... затем запускаю D10.")
    rec = subprocess.Popen(arecord_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(PLAYBACK_LEAD_SECONDS)
        aplay_cmd = [need_program("aplay"), "-q", "-D", DAC_DEVICE, "-t", "raw", "-f", fmt,
                     "-r", str(FS), "-c", "2", str(playback_path)]
        play = subprocess.run(aplay_cmd, capture_output=True, text=True)
        if play.returncode != 0:
            raise RuntimeError("Ошибка воспроизведения D10: " + (play.stderr or play.stdout).strip())
        _, stderr = rec.communicate(timeout=CAPTURE_SECONDS + 5)
        if rec.returncode != 0:
            raise RuntimeError("Ошибка записи E-MU: " + (stderr or "").strip())
    except Exception:
        if rec.poll() is None:
            rec.terminate()
            try:
                rec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                rec.kill()
        raise

    x = unpack_s24_3le(capture_path)
    if len(x) < FS * 30:
        raise RuntimeError(f"Получена слишком короткая запись: {len(x)/FS:.2f} s")
    wavfile.write(outdir / "emu-capture-float32.wav", FS, x.astype(np.float32))

    nwin = int(round(WINDOW_SECONDS * FS))
    hop = int(round(HOP_SECONDS * FS))
    nfft = 1 << int(math.ceil(math.log2(nwin)))
    win = blackmanharris(nwin, sym=False)
    freqs = np.fft.rfftfreq(nfft, 1.0 / FS)
    band_audio = (20.0, 20000.0)
    band_ultra = (40000.0, 96000.0)
    timeline = []
    for start in range(0, len(x) - nwin + 1, hop):
        centre_capture_s = (start + nwin / 2) / FS
        playback_s = centre_capture_s - PLAYBACK_LEAD_SECONDS
        state = "pre_capture"
        for label, a, b in SEGMENTS:
            if a <= playback_s < b:
                state = label
                break
        if playback_s >= SEGMENTS[-1][2]:
            state = "post_playback"
        row = {"time_capture_s": centre_capture_s, "time_playback_s": playback_s, "state": state}
        for ch, name in enumerate(("L", "R")):
            frame = x[start:start+nwin, ch]
            row[f"{name}_20-20k_dbfs"] = frame_spectrum(frame, FS, win, nfft, freqs, band_audio)
            row[f"{name}_40-96k_dbfs"] = frame_spectrum(frame, FS, win, nfft, freqs, band_ultra)
        timeline.append(row)

    import csv
    csv_path = outdir / "burst-timeline.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(timeline[0].keys()))
        writer.writeheader()
        writer.writerows(timeline)

    # Estimate tone amplitude in the central 4 s of each D10 active segment.
    capture_offset = PLAYBACK_LEAD_SECONDS
    tone_levels: dict[str, dict[str, float]] = {}
    for label, a, b, active_ch in (("D10_L_only", 3.0, 13.0, 0), ("D10_R_only", 17.0, 27.0, 1)):
        mid = (a + b) / 2 + capture_offset
        i0 = int((mid - 2.0) * FS)
        i1 = int((mid + 2.0) * FS)
        tone_levels[label] = {"L": tone_level(x[i0:i1, 0], FS), "R": tone_level(x[i0:i1, 1], FS)}

    report = []
    report.append("E-MU 0404 / SMSL D10 · TRANSIENT NOISE REPORT")
    report.append(f"Fs={FS} Hz · ADC format={ADC_FORMAT} · D10 format={fmt} · tone={TONE_HZ:.0f} Hz at {TONE_DBFS:.1f} dBFS")
    report.append("Band levels are windowed broadband/non-harmonic noise in dBFS, sine reference.")
    report.append("")
    report.append("STATE          ADC  tone level    20-20k median / p95 / max     40-96k median / p95 / max   >median+6dB")
    for state in ("idle_pre", "D10_L_only", "gap", "D10_R_only", "idle_post"):
        rows = [r for r in timeline if r["state"] == state]
        if not rows:
            continue
        for ch in ("L", "R"):
            a20 = [float(r[f"{ch}_20-20k_dbfs"]) for r in rows]
            a40 = [float(r[f"{ch}_40-96k_dbfs"]) for r in rows]
            m20, p9520, max20 = summarise(a20)
            m40, p9540, max40 = summarise(a40)
            base = float(np.median([float(r[f"{ch}_20-20k_dbfs"]) for r in timeline if r["state"] in ("idle_pre", "idle_post")]))
            bursts = sum(v > base + 6.0 for v in a20)
            tone = tone_levels.get(state, {}).get(ch, float("nan"))
            report.append(f"{state:13s} {ch} {tone:8.2f} dBFS   {m20:7.2f} / {p9520:7.2f} / {max20:7.2f}   {m40:7.2f} / {p9540:7.2f} / {max40:7.2f}   {bursts:3d}/{len(rows)}")
    report.append("")
    report.append("LARGEST 20-20k WINDOWS (candidate transient bursts):")
    all_base = {}
    for ch in ("L", "R"):
        base_rows = [r for r in timeline if r["state"] in ("idle_pre", "idle_post")]
        all_base[ch] = float(np.median([float(r[f"{ch}_20-20k_dbfs"]) for r in base_rows]))
        peaks = sorted(timeline, key=lambda r: float(r[f"{ch}_20-20k_dbfs"]), reverse=True)[:8]
        for r in peaks:
            report.append(f"  ADC {ch} t={r['time_playback_s']:6.2f}s state={r['state']:12s} 20-20k={r[f'{ch}_20-20k_dbfs']:7.2f} dBFS ({r[f'{ch}_20-20k_dbfs']-all_base[ch]:+.2f} dB over idle median)")
    report.append("")
    report.append("Interpretation: compare whether the bursts occur on ADC L/R regardless of which D10 channel is playing.")
    report.append("This test localizes the source side, but does not by itself prove which component is responsible.")

    report_path = outdir / "summary.txt"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\nФайлы сохранены: {outdir}")
    print("Основной результат: summary.txt; временной ряд: burst-timeline.csv; запись: emu-capture-float32.wav")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОтменено пользователем.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(2)
