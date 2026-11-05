#!/usr/bin/env python3
"""Matched-level D10 -> E-MU ADC steady-state test.

Run from ValveBench project root:
    python3 emu_d10_matched_steady.py
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
TARGET_TOL_DB = 1.0  # Вернули исходный допуск 5.0 дБ
DAC_DEVICE = "hw:2,0"  # STRIX RAID DLX (Loopback)
ADC_DEVICE = "hw:3,0"  # STRIX RAID DLX (Loopback)
ADC_FORMAT = "S24_3LE"
DAC_FORMAT = "S24_3LE"  # Изменено на 24-битный формат для ASUS
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
    return 10.0 * math.log10(max(2.0 * float(power), 1e-300))


def pack_s24_3le(x: np.ndarray) -> bytes:
    """Упаковывает float64 синусоиду в 3-байтовый (24-битный) PCM поток."""
    y = np.clip(np.rint(x * (2**23 - 1)), -(2**23), 2**23 - 1).astype("<i4")
    # Переходим от 4-байтового представления к 3-байтовому, отбрасывая пустой старший байт
    return y.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


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
    # Использует корректную упаковку в 24 бит (3LE) вместо 32 бит
    path.write_bytes(pack_s24_3le(x))


def play(path: Path) -> None:
    cmd = [need_program("aplay"), "-q", "-D", DAC_DEVICE, "-t", "raw", "-f", DAC_FORMAT,
           "-r", str(FS), "-c", "2", str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("Ошибка воспроизведения ASUS STRIX: " + (p.stderr or p.stdout).strip())


def capture_with_playback(play_path: Path, capture_path: Path, capture_seconds: int) -> None:
    cmd = [need_program("arecord"), "-q", "-D", ADC_DEVICE, "-t", "raw", "-f", ADC_FORMAT,
           "-r", str(FS), "-c", "2", "-d", str(capture_seconds), str(capture_path)]
    rec = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(LEAD)
        play(play_path)
        _, err = rec.communicate(timeout=capture_seconds + 5)
        if rec.returncode:
            raise RuntimeError("Ошибка записи ASUS STRIX: " + (err or "").strip())
    except Exception:
        if rec.poll() is None:
            rec.terminate()
            try:
                rec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                rec.kill()
        raise


def fit_tone(y: np.ndarray, initial_hz: float = TONE_HZ) -> tuple[float, float]:
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
    tmp_in = outdir / f"calib_in_{trial}.raw"
    tmp_out = outdir / f"calib_out_{trial}.raw"
    
    x = tone_file(2.5, "both")
    write_s32(tmp_out, x)
    
    try:
        capture_with_playback(tmp_out, tmp_in, 3)
        y = unpack_s24_3le(tmp_in)
        if len(y) < FS:
            return -120.0, -120.0
        
        start_idx = int(1.0 * FS)
        end_idx = int(2.0 * FS)
        
        _, v_l = fit_tone(y[start_idx:end_idx, 0])
        _, v_r = fit_tone(y[start_idx:end_idx, 1])
        return v_l, v_r
    finally:
        if tmp_in.exists(): tmp_in.unlink()
        if tmp_out.exists(): tmp_out.unlink()


def generate_full_test_signal() -> np.ndarray:
    """Генерирует тестовую последовательность согласно SEGMENTS (95 секунд)."""
    total_n = int(round(PLAY_SECONDS * FS))
    x = np.zeros((total_n, 2), dtype=np.float64)
    t = np.arange(total_n, dtype=np.float64) / FS
    amp = 10.0 ** (TONE_DBFS / 20.0)
    
    # Левый канал
    s_l = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
    mask_l_in = (t >= 3.0) & (t < 4.0)
    s_l[mask_l_in] *= (t[mask_l_in] - 3.0)
    mask_l_out = (t >= 44.0) & (t < 45.0)
    s_l[mask_l_out] *= (45.0 - t[mask_l_out])
    s_l[t < 3.0] = 0.0
    s_l[t >= 45.0] = 0.0
    x[:, 0] = s_l

    # Правый канал
    s_r = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
    mask_r_in = (t >= 50.0) & (t < 51.0)
    s_r[mask_r_in] *= (t[mask_r_in] - 50.0)
    mask_r_out = (t >= 91.0) & (t < 92.0)
    s_r[mask_r_out] *= (92.0 - t[mask_r_out])
    s_r[t < 50.0] = 0.0
    s_r[t >= 92.0] = 0.0
    x[:, 1] = s_r

    return x


def main():
    print("ASUS STRIX RAID DLX · MATCHED-LEVEL LOOPBACK TEST")
    print(f"ASUS DAC={DAC_DEVICE} → ASUS ADC={ADC_DEVICE} · Fs={FS} · Format={DAC_FORMAT}")
    print(f"Цель: {TARGET_ADC_DBFS} dBFS на каждом входе (допуск ±{TARGET_TOL_DB} dB).")
    
    outdir = Path("temp")
    outdir.mkdir(exist_ok=True)
    
    trial = 1
    while True:
        print("Нажмите Enter для запуска проверки уровня звукового тракта (или 'q' для выхода): ", end="")
        choice = input().strip().lower()
        if choice == 'q':
            sys.exit(0)
            
        print("  Короткий контрольный тон на обоих каналах карты...")
        l_dbfs, r_dbfs = calibration_trial(outdir, trial)
        trial += 1
        
        dev_l = l_dbfs - TARGET_ADC_DBFS
        dev_r = r_dbfs - TARGET_ADC_DBFS
        
        print(f"  ADC L={l_dbfs:.2f} dBFS ({dev_l:+.2f} dB до цели) · ADC R={r_dbfs:.2f} dBFS ({dev_r:+.2f} dB до цели)")
        
        if abs(dev_l) <= TARGET_TOL_DB and abs(dev_r) <= TARGET_TOL_DB:
            print(" Уровень успешно согласован! Переходим к основному тесту.")
            break
        else:
            req = (dev_l + dev_r) / 2.0
            action = "УМЕНЬШИ" if req > 0 else "УВЕЛИЧЬ"
            print(f"  {action} системный/аппаратный микшер карты примерно на {abs(req):.1f} dB и повторите.")

    # --- ОСНОВНОЙ ДЛИННЫЙ ТЕСТ ---
    print("\n[1/3] Накапливаем основной тестовый сигнал (95 сек)...")
    full_signal = generate_full_test_signal()
    play_file = outdir / "main_playback.raw"
    cap_file = outdir / "main_capture.raw"
    write_s32(play_file, full_signal)
    
    print("[2/3] Запуск одновременного Full-Duplex прогона...")
    print("Пожалуйста, подождите 97 секунд...")
    capture_with_playback(play_file, cap_file, CAPTURE_SECONDS)
    
    print("[3/3] Декодирование и БПФ-анализ по временным окнам...")
    y_captured = unpack_s24_3le(cap_file)
    
    # Скользящий спектральный оконный анализ
    w_size = int(round(WINDOW_SECONDS * FS))
    w_hop = int(round(HOP_SECONDS * FS))
    win = blackmanharris(w_size)
    
    # Сначала определяем точные частоты на участках L_steady и R_steady
    # Поиск частоты тона для L
    idx_l_steady_start = int(round((4.0 + LEAD) * FS))
    idx_l_steady_end = int(round((44.0 + LEAD) * FS))
    f0_l, adc_l_steady_level = fit_tone(y_captured[idx_l_steady_start:idx_l_steady_end, 0])

    # Поиск частоты тона для R
    idx_r_steady_start = int(round((51.0 + LEAD) * FS))
    idx_r_steady_end = int(round((91.0 + LEAD) * FS))
    f0_r, adc_r_steady_level = fit_tone(y_captured[idx_r_steady_start:idx_r_steady_end, 1])

    # Считаем базовый шум (Idle) на участке idle_pre
    idx_idle_start = int(round((0.0 + LEAD) * FS))
    idx_idle_end = int(round((3.0 + LEAD) * FS))
    idle_windows = (idx_idle_end - idx_idle_start - w_size) // w_hop
    
    idle_powers_20_20k = {"L": [], "R": []}
    for w in range(idle_windows):
        off = idx_idle_start + w * w_hop
        for ch_idx, ch_name in ((0, "L"), (1, "R")):
            freqs, psd = window_psd(y_captured[off:off+w_size, ch_idx], win, w_size, FS)
            mask_20_20k = (freqs >= 20.0) & (freqs <= 20000.0)
            p_band = np.sum(psd[mask_20_20k]) * (freqs[1] - freqs[0])
            idle_powers_20_20k[ch_name].append(p_band)
            
    idle_baseline_l = dbfs_from_power(np.median(idle_powers_20_20k["L"]))
    idle_baseline_r = dbfs_from_power(np.median(idle_powers_20_20k["R"]))

    # Полный прогон по всем окнам всей записи
    total_frames = (len(y_captured) - w_size) // w_hop
    results = []
    all_excursions = [] # Для сбора топ-вылетов

    for idx in range(total_frames):
        offset = idx * w_hop
        t_current = (offset + w_size / 2) / FS - LEAD
        segment_name = state_at(t_current)
        
        for ch_idx, ch_name in ((0, "L"), (1, "R")):
            freqs, psd = window_psd(y_captured[offset:offset+w_size, ch_idx], win, w_size, FS)
            df = freqs[1] - freqs[0]
            
            # Полоса 20 Гц - 20 кГц
            mask_20_20k = (freqs >= 20.0) & (freqs <= 20000.0)
            # Фильтруем основной тон 997 Гц (вырезаем окно ±15 Гц), чтобы получить чистый шум
            mask_tone = (freqs >= 997.0 - 15.0) & (freqs <= 997.0 + 15.0)
            mask_noise_20_20k = mask_20_20k & (~mask_tone)
            
            p_20_20k = np.sum(psd[mask_noise_20_20k]) * df
            db_20_20k = dbfs_from_power(p_20_20k)
            
            # Полоса 40 кГц - 96 кГц
            mask_40_96k = (freqs >= 40000.0) & (freqs <= 96000.0)
            p_40_96k = np.sum(psd[mask_40_96k]) * df
            db_40_96k = dbfs_from_power(p_40_96k)
            
            results.append({
                "segment": segment_name,
                "ch": ch_name,
                "db_20_20k": db_20_20k,
                "db_40_96k": db_40_96k,
                "t": t_current
            })
            
            # Собираем данные для топа вылетов (интересует только поведение канала L во время steady стадий)
            if segment_name in ("L_steady", "R_steady") and ch_name == "L":
                baseline = idle_baseline_l
                diff = db_20_20k - baseline
                all_excursions.append((t_current, segment_name, db_20_20k, diff))

    # Печать красивого финального текстового отчета в консоль
    print("\n" + "="*80)
    print("MEASURED TONE / CHANNEL MAP:")
    print(f"  ASUS_L_only: ADC L={adc_l_steady_level:.2f} dBFS at {f0_l:.6f} Hz")
    print(f"  ASUS_R_only: ADC R={adc_r_steady_level:.2f} dBFS at {f0_r:.6f} Hz")
    print(f"  f0 difference={abs(f0_l - f0_r):.6f} Hz")
    print(f"IDLE BASELINE: L={idle_baseline_l:.2f} dBFS · R={idle_baseline_r:.2f} dBFS")
    
    print(f"{'STATE':11s} {'ADC':4s} {'20-20k median / p95 / max dBFS':36s} {' >baseline+6dB':17s} {'40-96k median / p95 / max dBFS'}")
    
    for seg in ("L_steady", "R_steady"):
        for ch in ("L", "R"):
            sub = [r for r in results if r["segment"] == seg and r["ch"] == ch]
            if not sub: continue
            
            v_20 = [r["db_20_20k"] for r in sub]
            v_40 = [r["db_40_96k"] for r in sub]
            
            med_20, p95_20, max_20 = np.median(v_20), np.percentile(v_20, 95), np.max(v_20)
            med_40, p95_40, max_40 = np.median(v_40), np.percentile(v_40, 95), np.max(v_40)
            
            base = idle_baseline_l if ch == "L" else idle_baseline_r
            over_count = sum(1 for v in v_20 if v > base + 6.0)
            
            print(f"{seg:11s} {ch:4s} {med_20:7.2f} / {p95_20:7.2f} / {max_20:7.2f}      {over_count:3d}/{len(v_20):3d}      {med_40:7.2f} / {p95_40:7.2f} / {max_40:7.2f}")

    print("\nTOP WINDOWS DURING STEADY TONE:")
    # Сортируем вылеты канала L по убыванию уровня (худшие сверху) и берем топ-20
    all_excursions.sort(key=lambda x: x[2], reverse=True)
    for t_val, seg_val, db_val, diff_val in all_excursions[:20]:
        print(f"  ADC L t={t_val:6.2f}s {seg_val:10s} {db_val:7.2f} dBFS (+{diff_val:.2f} dB vs idle)")

    print("\nInterpretation: compare this report with the E-MU internal-loopback corrected analysis at the same ADC tone level.")
    print("If left-channel excursions persist with D10 at matched level, suspect ADC-L/input-local coupling.")
    print("If D10 is clean at matched level but E-MU internal DAC loopback is not, suspicion shifts to DAC-L/output path or its interaction with the input.")
    print("This test alone cannot identify a specific PCB component.")
    print("="*80)

    # Запись дублирующего лога в файл для истории
    csv_path = Path("strix_steady_report.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["time_sec", "segment", "ch", "db_20_20k", "db_40_96k"])
        writer.writeheader()
        for r in results:
            writer.writerow({"time_sec": r["t"], "segment": r["segment"], "ch": r["ch"], "db_20_20k": r["db_20_20k"], "db_40_96k": r["db_40_96k"]})

    if play_file.exists(): play_file.unlink()
    if cap_file.exists(): cap_file.unlink()


if __name__ == '__main__':
    main()

