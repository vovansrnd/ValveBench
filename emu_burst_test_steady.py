#!/usr/bin/env python3
"""Steady-state transient test: SMSL D10 playback -> E-MU 0404 ADC.

Run from the ValveBench project directory:
    python3 emu_burst_test_steady.py

Unlike emu_burst_test.py, this test does NOT abruptly gate a tone at the
measurement boundaries. Each D10 channel plays a long 997 Hz tone with 1 s
raised-cosine fades. Burst statistics are calculated only in the steady
parts, away from all fades. This avoids reporting the test's own switching
edges as spontaneous noise bursts.
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
from scipy.signal.windows import blackmanharris

FS = 192000
TONE_HZ = 997.0
TONE_DBFS = -18.0
DAC_DEVICE = "hw:3,0"   # SMSL D10
ADC_DEVICE = "hw:2,0"   # E-MU 0404
ADC_FORMAT = "S24_3LE"
PLAYBACK_LEAD_SECONDS = 1.0
CAPTURE_SECONDS = 96
FADE_SECONDS = 1.0
WINDOW_SECONDS = 0.100
HOP_SECONDS = 0.050
HARMONIC_EXCLUSION_HZ = 60.0

# Time positions are measured from the start of the D10 playback file.
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
        raise RuntimeError(f"Не найдена команда {name!r} (пакет alsa-utils).")
    return p


def dbfs_from_power(power: float) -> float:
    # Integrated power is converted to a full-scale-sine reference.
    return 10.0 * math.log10(max(2.0 * float(power), 1e-300))


def pack_s32(x: np.ndarray) -> bytes:
    y = np.clip(np.rint(x * (2**31 - 1)), -(2**31), 2**31 - 1).astype("<i4")
    return y.tobytes()


def make_playback() -> np.ndarray:
    total = int(95 * FS)
    x = np.zeros((total, 2), dtype=np.float64)
    amp = 10.0 ** (TONE_DBFS / 20.0)
    for channel, start, end in ((0, 3.0, 45.0), (1, 50.0, 92.0)):
        n = int(round((end - start) * FS))
        t = np.arange(n, dtype=np.float64) / FS
        tone = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
        fade_n = int(round(FADE_SECONDS * FS))
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade_n, endpoint=True))
        envelope = np.ones(n, dtype=np.float64)
        envelope[:fade_n] = ramp
        envelope[-fade_n:] = ramp[::-1]
        x[int(start * FS):int(end * FS), channel] = tone * envelope
    return x


def probe_d10(fmt: str, probe_file: Path) -> tuple[bool, str]:
    bytes_per_sample = 3 if fmt == "S24_3LE" else 4
    probe_file.write_bytes(bytes(int(0.1 * FS) * 2 * bytes_per_sample))
    cmd = [need_program("aplay"), "-q", "-D", DAC_DEVICE, "-t", "raw", "-f", fmt,
           "-r", str(FS), "-c", "2", str(probe_file)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return False, "проба воспроизведения зависла"
    if p.returncode == 0:
        return True, "OK"
    return False, (p.stderr or p.stdout or f"код {p.returncode}").strip()


def write_playback(path: Path, x: np.ndarray, fmt: str) -> None:
    if fmt == "S32_LE":
        path.write_bytes(pack_s32(x))
    elif fmt == "S24_3LE":
        p = np.clip(np.rint(x * (2**23 - 1)), -(2**23), 2**23 - 1).astype(np.int32)
        u = p & 0xFFFFFF
        packed = np.empty((len(p), 2, 3), dtype=np.uint8)
        packed[:, :, 0] = u & 0xFF
        packed[:, :, 1] = (u >> 8) & 0xFF
        packed[:, :, 2] = (u >> 16) & 0xFF
        packed.tofile(path)
    elif fmt == "S24_LE":
        p = np.clip(np.rint(x * (2**23 - 1)), -(2**23), 2**23 - 1).astype(np.int32)
        (p << 8).astype("<i4").tofile(path)
    else:
        raise ValueError(f"Неподдерживаемый формат D10: {fmt}")


def unpack_s24_3le(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    frame_bytes = 2 * 3
    raw = raw[: raw.size - (raw.size % frame_bytes)]
    b = raw.reshape(-1, 2, 3).astype(np.int32)
    u = b[:, :, 0] | (b[:, :, 1] << 8) | (b[:, :, 2] << 16)
    signed = (u ^ 0x800000) - 0x800000
    return signed.astype(np.float64) / float(2**23)


def frame_band_level(y: np.ndarray, window: np.ndarray, nfft: int,
                     freqs: np.ndarray, fs: int, band: tuple[float, float]) -> float:
    y = np.asarray(y, dtype=np.float64)
    y = y - np.mean(y)
    spec = np.fft.rfft(y * window, n=nfft)
    psd = (np.abs(spec) ** 2) / (fs * np.sum(window ** 2))
    if nfft > 1:
        psd[1:-1] *= 2.0
    df = fs / nfft
    lo, hi = band
    mask = (freqs >= lo) & (freqs < hi)
    # Exclude the 997 Hz fundamental and harmonics, including their skirts.
    k0 = max(1, int(lo // TONE_HZ))
    k1 = int(hi // TONE_HZ)
    for k in range(k0, k1 + 1):
        f = k * TONE_HZ
        mask &= np.abs(freqs - f) > HARMONIC_EXCLUSION_HZ
    return dbfs_from_power(float(np.sum(psd[mask]) * df))


def tone_level(y: np.ndarray) -> float:
    n = len(y)
    t = np.arange(n, dtype=np.float64) / FS
    s = np.sin(2.0 * np.pi * TONE_HZ * t)
    c = np.cos(2.0 * np.pi * TONE_HZ * t)
    yy = np.asarray(y, dtype=np.float64) - np.mean(y)
    a = 2.0 * np.dot(yy, s) / n
    b = 2.0 * np.dot(yy, c) / n
    peak = math.hypot(float(a), float(b))
    return 20.0 * math.log10(max(peak, 1e-30))


def state_at(t: float) -> str:
    for name, start, end in SEGMENTS:
        if start <= t < end:
            return name
    if t < 0:
        return "pre_capture"
    return "post_playback"


def median(values: list[float]) -> float:
    return float(np.median(np.asarray(values, dtype=np.float64)))


def main() -> int:
    need_program("arecord")
    need_program("aplay")
    root = Path.cwd()
    outdir = root / "emu_measurements" / f"D10-steady-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    print("E-MU 0404 / SMSL D10 · STEADY-STATE BURST TEST")
    print(f"D10={DAC_DEVICE} · E-MU={ADC_DEVICE} · Fs={FS} Hz · tone={TONE_HZ:.0f} Hz / {TONE_DBFS:.1f} dBFS")
    print("Подключение: D10 3.5-mm stereo -> 2xRCA -> E-MU L/R; белый=L, красный=R.")
    print("E-MU: оба входа Grounded Input, phantom OFF. D10: только 3.5-mm выход, не 4.4-mm balanced.")
    print("Громкость D10 не менять между опытами. Скрипт запишет около 96 секунд.")
    print("Тоны плавно включаются/выключаются; пики на границах fade не учитываются в статистике steady.")
    input("Подключи кабели и нажми Enter; Ctrl+C отменяет: ")

    probe = outdir / "d10-format-probe.raw"
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
    print(f"D10 открыт: 192 кГц, {fmt}.")

    playback = make_playback()
    playback_path = outdir / "d10-stimulus.raw"
    write_playback(playback_path, playback, fmt)

    capture_path = outdir / "emu-capture-s24_3le.raw"
    rec_cmd = [need_program("arecord"), "-q", "-D", ADC_DEVICE, "-t", "raw", "-f", ADC_FORMAT,
               "-r", str(FS), "-c", "2", "-d", str(CAPTURE_SECONDS), str(capture_path)]
    print("Начинаю запись E-MU, через секунду запускаю D10...")
    rec = subprocess.Popen(rec_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(PLAYBACK_LEAD_SECONDS)
        play_cmd = [need_program("aplay"), "-q", "-D", DAC_DEVICE, "-t", "raw", "-f", fmt,
                    "-r", str(FS), "-c", "2", str(playback_path)]
        played = subprocess.run(play_cmd, capture_output=True, text=True)
        if played.returncode != 0:
            raise RuntimeError("Ошибка воспроизведения D10: " + (played.stderr or played.stdout).strip())
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
    expected = FS * 90
    if len(x) < expected:
        raise RuntimeError(f"Получена слишком короткая запись: {len(x)/FS:.2f} s; ожидалось не менее 90 s")
    wavfile.write(outdir / "emu-capture-float32.wav", FS, x.astype(np.float32))

    nwin = int(round(WINDOW_SECONDS * FS))
    hop = int(round(HOP_SECONDS * FS))
    nfft = 1 << int(math.ceil(math.log2(nwin)))
    win = blackmanharris(nwin, sym=False)
    freqs = np.fft.rfftfreq(nfft, 1.0 / FS)
    rows = []
    for start in range(0, len(x) - nwin + 1, hop):
        capture_centre = (start + nwin / 2) / FS
        playback_centre = capture_centre - PLAYBACK_LEAD_SECONDS
        state = state_at(playback_centre)
        row = {"capture_time_s": capture_centre, "playback_time_s": playback_centre, "state": state}
        for ch, name in enumerate(("L", "R")):
            frame = x[start:start+nwin, ch]
            row[f"{name}_20-20k_dbfs"] = frame_band_level(frame, win, nfft, freqs, FS, (20.0, 20000.0))
            row[f"{name}_40-96k_dbfs"] = frame_band_level(frame, win, nfft, freqs, FS, (40000.0, 96000.0))
        rows.append(row)

    # Tone levels from central 8 seconds of each stable interval.
    tone_readings = {}
    for label, centre in (("D10_L_only", 24.0), ("D10_R_only", 71.0)):
        cap_centre = centre + PLAYBACK_LEAD_SECONDS
        i0 = int((cap_centre - 4.0) * FS)
        i1 = int((cap_centre + 4.0) * FS)
        tone_readings[label] = {"ADC_L": tone_level(x[i0:i1, 0]), "ADC_R": tone_level(x[i0:i1, 1])}

    idle_states = {"idle_pre", "idle_gap", "idle_post"}
    stable_states = {"L_steady", "R_steady"}
    baseline = {}
    for ch in ("L", "R"):
        baseline[ch] = median([float(r[f"{ch}_20-20k_dbfs"]) for r in rows if r["state"] in idle_states])

    def stats_for(ch: str, state: str, band: str) -> tuple[float, float, float, int, int]:
        vals = [float(r[f"{ch}_{band}_dbfs"]) for r in rows if r["state"] == state]
        if not vals:
            return (float("nan"),) * 3 + (0, 0)
        base = baseline[ch] if band == "20-20k" else median([
            float(r[f"{ch}_40-96k_dbfs"]) for r in rows if r["state"] in idle_states
        ])
        return median(vals), float(np.percentile(vals, 95)), max(vals), sum(v > base + 6.0 for v in vals), len(vals)

    report = [
        "E-MU 0404 / SMSL D10 · STEADY-STATE TRANSIENT REPORT",
        f"Fs={FS} Hz · ADC={ADC_FORMAT} · D10={fmt} · tone={TONE_HZ:.0f} Hz at {TONE_DBFS:.1f} dBFS",
        f"Window={WINDOW_SECONDS*1000:.0f} ms · hop={HOP_SECONDS*1000:.0f} ms · fade={FADE_SECONDS:.1f} s",
        "All burst statistics below use steady tone intervals only; fade edges are excluded.",
        "",
        "CHANNEL MAPPING (tone should be much stronger on the matching ADC channel):",
    ]
    for label, values in tone_readings.items():
        lval, rval = values["ADC_L"], values["ADC_R"]
        direct = "ADC L" if lval > rval else "ADC R"
        delta = abs(lval - rval)
        if delta < 12.0:
            verdict = "AMBIGUOUS: channel separation <12 dB"
        elif (label == "D10_L_only" and direct == "ADC L") or (label == "D10_R_only" and direct == "ADC R"):
            verdict = "ALIGNED"
        else:
            verdict = "CROSSED"
        report.append(f"  {label:10s}: ADC L={lval:8.2f} dBFS · ADC R={rval:8.2f} dBFS · {verdict}")

    report.extend([
        "",
        f"IDLE BASELINE 20-20k: ADC L={baseline['L']:.2f} dBFS · ADC R={baseline['R']:.2f} dBFS",
        "STEADY WINDOWS: median / p95 / max; bursts = windows > idle baseline +6 dB",
        "STATE       ADC    20-20k median / p95 / max dBFS       bursts      40-96k median / p95 / max dBFS",
    ])
    for state in ("L_steady", "R_steady"):
        for ch in ("L", "R"):
            m, p95, mx, count, total = stats_for(ch, state, "20-20k")
            u_m, u_p95, u_mx, _, _ = stats_for(ch, state, "40-96k")
            report.append(f"{state:10s} {ch}    {m:7.2f} / {p95:7.2f} / {mx:7.2f}       {count:3d}/{total:<3d}      {u_m:7.2f} / {u_p95:7.2f} / {u_mx:7.2f}")

    report.append("")
    report.append("TOP STEADY-STATE 20-20k WINDOWS (not fades):")
    for ch in ("L", "R"):
        candidates = [r for r in rows if r["state"] in stable_states]
        top = sorted(candidates, key=lambda r: float(r[f"{ch}_20-20k_dbfs"]), reverse=True)[:10]
        for r in top:
            report.append(f"  ADC {ch} t={r['playback_time_s']:6.2f}s state={r['state']:8s} level={r[f'{ch}_20-20k_dbfs']:7.2f} dBFS ({r[f'{ch}_20-20k_dbfs']-baseline[ch]:+.2f} dB vs idle)")

    report += [
        "",
        "Interpretation guide:",
        "- If elevated windows occur during a steady tone, they are not caused by tone on/off edges.",
        "- If they follow D10_L_only/D10_R_only and move with the cable mapping, suspect the corresponding D10 output/cable path.",
        "- If they remain on the same ADC input with either D10 output active, suspect that ADC input path or local coupling.",
        "- This test alone cannot identify a specific component on the PCB.",
    ]

    (outdir / "summary.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    with (outdir / "burst-timeline.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
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
        print("\nОтменено пользователем.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(2)
