#!/usr/bin/env python3
"""One-command A/B measurement workflow for E-MU 0404 USB / ValveBench 0.1.7.

Usage:
  python3 emu_bench.py          # measure and compare; do not keep files
  python3 emu_bench.py A1       # measure, compare, and archive this hardware state
  python3 emu_bench.py A2       # compare against the previous archived state
  python3 emu_bench.py A2 --against A0

The script intentionally pauses for the two physical connection states:
  1) ADC inputs shorted, DAC-to-ADC cables disconnected: ADC-only noise.
  2) Shorting plugs removed, DAC L/R connected to ADC L/R: loopback and crosstalk.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import welch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench import calib, vinyl  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402
from valvebench.measure import Rig  # noqa: E402
from valvebench.looptest import run_loopback_report  # noqa: E402

# A0 is the user's measured pre-modification state, reconstructed from the
# reports pasted in the conversation. No raw A0 WAV/CSV was supplied.
BASELINE_A0: dict[str, Any] = {
    "label": "A0",
    "description": "Исходная E-MU 0404: USB power mod, existing left trace cut/jumper, Grounded Input.",
    "source_note": "A0 values transcribed from the user's 192 kHz measurements; ADC-only noise and detailed xtalk are measured baselines.",
    "adc_only": {"channels": {
        "L": {
            "full_rms_dbfs": -81.95,
            "bands": {"0-10": -126.11, "10-20": -126.64, "20-20k": -110.50,
                      "20-40k": -108.82, "40-60k": -98.98, "60-80k": -88.52, "80-96k": -83.15},
        },
        "R": {
            "full_rms_dbfs": -81.93,
            "bands": {"0-10": -125.28, "10-20": -126.19, "20-20k": -110.54,
                      "20-40k": -108.94, "40-60k": -98.77, "60-80k": -88.51, "80-96k": -83.13},
        },
    }},
    "xtalk": {
        "100": {
            "L->R": {"main_dbfs": -11.89, "leak_dbfs": -128.39, "xtalk_db": -116.50, "coherence": 0.993},
            "R->L": {"main_dbfs": -11.71, "leak_dbfs": -122.41, "xtalk_db": -110.70, "coherence": 0.994},
        },
        "1000": {
            "L->R": {"main_dbfs": -11.86, "leak_dbfs": -116.19, "xtalk_db": -104.32, "coherence": 1.000},
            "R->L": {"main_dbfs": -11.68, "leak_dbfs": -116.01, "xtalk_db": -104.33, "coherence": 1.000},
        },
        "10000": {
            "L->R": {"main_dbfs": -11.89, "leak_dbfs": -97.19, "xtalk_db": -85.30, "coherence": 1.000},
            "R->L": {"main_dbfs": -11.71, "leak_dbfs": -97.44, "xtalk_db": -85.72, "coherence": 1.000},
        },
    },
    "loopback": {
        "-1": {
            "L": {"adc_dbfs": -6.87, "loop_gain_db": -5.87, "thd_pct": 0.001389, "thdn_dbc": -85.19,
                  "noise_only_dbfs": -92.35, "H2_dbc": -103.2, "H3_dbc": -98.4},
            "R": {"adc_dbfs": -6.69, "loop_gain_db": -5.69, "thd_pct": 0.001199, "thdn_dbc": -97.14,
                  "noise_only_dbfs": -109.75, "H2_dbc": -113.1, "H3_dbc": -98.6},
        },
        "-3": {
            "L": {"adc_dbfs": -8.87, "loop_gain_db": -5.87, "thd_pct": 0.000903, "thdn_dbc": -86.94,
                  "noise_only_dbfs": -96.02, "H2_dbc": -107.1, "H3_dbc": -102.2},
            "R": {"adc_dbfs": -8.69, "loop_gain_db": -5.69, "thd_pct": 0.000799, "thdn_dbc": -98.56,
                  "noise_only_dbfs": -109.94, "H2_dbc": -116.4, "H3_dbc": -102.3},
        },
        "-6": {
            "L": {"adc_dbfs": -11.87, "loop_gain_db": -5.87, "thd_pct": 0.000635, "thdn_dbc": -86.82,
                  "noise_only_dbfs": -98.78, "H2_dbc": -118.4, "H3_dbc": -104.2},
            "R": {"adc_dbfs": -11.69, "loop_gain_db": -5.69, "thd_pct": 0.000633, "thdn_dbc": -97.34,
                  "noise_only_dbfs": -110.09, "H2_dbc": -116.0, "H3_dbc": -104.4},
        },
        "-9": {
            "L": {"adc_dbfs": -14.87, "loop_gain_db": -5.87, "thd_pct": 0.000445, "thdn_dbc": -86.62,
                  "noise_only_dbfs": -101.55, "H2_dbc": -123.3, "H3_dbc": -107.3},
            "R": {"adc_dbfs": -14.69, "loop_gain_db": -5.69, "thd_pct": 0.000522, "thdn_dbc": -95.10,
                  "noise_only_dbfs": -110.20, "H2_dbc": -114.1, "H3_dbc": -106.4},
        },
        "-12": {
            "L": {"adc_dbfs": -17.87, "loop_gain_db": -5.87, "thd_pct": 0.000410, "thdn_dbc": -86.05,
                  "noise_only_dbfs": -103.97, "H2_dbc": -116.4, "H3_dbc": -109.3},
            "R": {"adc_dbfs": -17.69, "loop_gain_db": -5.69, "thd_pct": 0.000385, "thdn_dbc": -92.35,
                  "noise_only_dbfs": -110.18, "H2_dbc": -113.9, "H3_dbc": -109.8},
        },
    },
}

FS = 192000
BLOCKS = 8
ADC_SECONDS = 8.0
LOOP_LEVELS = [-1.0, -3.0, -6.0, -9.0, -12.0]
LOOP_REPEATS = 3
LOOP_SILENCE_SECONDS = 5.0
XTALK_FREQS = [100.0, 1000.0, 10000.0]
XTALK_LEVEL = -6.0
XTALK_REPEATS = 3

BANDS = [
    ("0-10", "0–10 Гц", 0.0, 10.0),
    ("10-20", "10–20 Гц", 10.0, 20.0),
    ("20-20k", "20 Гц–20 кГц", 20.0, 20000.0),
    ("20-40k", "20–40 кГц", 20000.0, 40000.0),
    ("40-60k", "40–60 кГц", 40000.0, 60000.0),
    ("60-80k", "60–80 кГц", 60000.0, 80000.0),
    ("80-96k", "80–96 кГц", 80000.0, 96000.0),
]


def db10(v: float) -> float:
    return 10.0 * math.log10(max(float(v), 1e-300))


def db20(v: float) -> float:
    return 20.0 * math.log10(max(float(v), 1e-300))


def ask_enter(message: str) -> None:
    print("\n" + "=" * 76)
    print(message)
    input("Когда всё подключено правильно, нажми Enter. Для отмены — Ctrl+C: ")


def measure_adc_only(rig: Rig, output_wav: Path) -> tuple[dict[str, Any], list[str]]:
    print("\n[1/3] Записываю АЦП без работающего ЦАП: 8 секунд, 192 кГц...")
    x = rig.be.record(ADC_SECONDS)
    notes = list(getattr(rig.be, "notes", []))
    vinyl.write_wav(output_wav, rig.fs, x)
    result: dict[str, Any] = {"fs": rig.fs, "seconds": len(x) / rig.fs, "channels": {}}
    spectra: dict[str, np.ndarray] = {}
    common_freq: np.ndarray | None = None

    nperseg = 131072
    for ch, name in ((0, "L"), (1, "R")):
        y = np.asarray(x[:, ch], dtype=np.float64)
        dc = float(np.mean(y))
        ac = y - dc
        rms = float(np.sqrt(np.mean(ac * ac)))
        full_rms = db20(math.sqrt(2.0) * rms)
        f, p = welch(ac, fs=rig.fs, window="blackmanharris", nperseg=nperseg,
                     noverlap=nperseg // 2, scaling="density", detrend=False)
        df = float(f[1] - f[0])
        asd = 10.0 * np.log10(np.maximum(2.0 * p, 1e-300))
        common_freq = f
        spectra[name] = asd
        ch_data: dict[str, Any] = {"dc_dbfs": db20(abs(dc)), "full_rms_dbfs": full_rms, "bands": {}}
        for key, _label, lo, hi in BANDS:
            mask = (f >= lo) & (f <= hi)
            power = float(np.sum(p[mask]) * df)
            spectral_mask = (f >= lo) & (f < hi)
            median_asd = float(np.median(asd[spectral_mask])) if np.any(spectral_mask) else None
            ch_data["bands"][key] = {
                "integrated_dbfs": db10(2.0 * power),
                "median_asd_dbfs_sqrt_hz": median_asd,
            }
        # Independently compare integrated PSD against time-domain RMS.
        all_mask = (f >= 0.0) & (f <= rig.fs / 2.0)
        psd_rms = db10(2.0 * float(np.sum(p[all_mask]) * df))
        ch_data["psd_vs_time_delta_db"] = psd_rms - full_rms
        result["channels"][name] = ch_data

    # Keep the complete ADC-only spectrum in every archived state, not only band totals.
    spectrum_path = output_wav.with_name("adc-only-spectrum.csv")
    with spectrum_path.open("w", newline="", encoding="utf-8") as f_csv:
        writer = csv.writer(f_csv)
        writer.writerow(["frequency_hz", "L_dBFS_sqrt_Hz", "R_dBFS_sqrt_Hz"])
        for i, hz in enumerate(common_freq):
            writer.writerow([float(hz), float(spectra["L"][i]), float(spectra["R"][i])])
    return result, notes


def measure_xtalk(rig: Rig) -> tuple[dict[str, Any], list[str]]:
    print("\n[3/3] Измеряю проникновение L↔R: 100 Гц, 1 кГц, 10 кГц; по 3 повтора...")
    out: dict[str, Any] = {"level_dbfs": XTALK_LEVEL, "repeats": XTALK_REPEATS, "frequencies": {}}
    warnings: list[str] = []

    def analyse_window(win: np.ndarray, k: int, driven_ch: int) -> dict[str, float]:
        blocks = np.asarray(win[:BLOCKS * rig.n], dtype=np.float64).reshape(BLOCKS, rig.n, 2)
        z = (2.0 / rig.n) * np.fft.rfft(blocks, axis=1)[:, k, :]
        source = z[:, driven_ch]
        victim = z[:, 1 - driven_ch]
        source_unit = source / np.maximum(np.abs(source), 1e-30)
        aligned = victim * np.conj(source_unit)
        main_amp = float(np.mean(np.abs(source)))
        leak_amp = float(np.abs(np.mean(aligned)))
        coherence = float(leak_amp / (np.sqrt(np.mean(np.abs(aligned) ** 2)) + 1e-30))
        main_db = db20(main_amp)
        leak_db = db20(leak_amp)
        return {"main_dbfs": main_db, "leak_dbfs": leak_db,
                "xtalk_db": leak_db - main_db, "coherence": coherence}

    for freq in XTALK_FREQS:
        k, amp = rig.amp_tone(freq, XTALK_LEVEL)
        freq_key = str(int(freq))
        out["frequencies"][freq_key] = {"L->R": [], "R->L": []}
        for rep in range(1, XTALK_REPEATS + 1):
            segs = [([(k, amp)], [], None), ([], [(k, amp)], None)]
            wins, warns = rig.run(segs)
            warnings.extend(warns)
            ltr = analyse_window(wins[0], k, 0)
            rtl = analyse_window(wins[1], k, 1)
            out["frequencies"][freq_key]["L->R"].append(ltr)
            out["frequencies"][freq_key]["R->L"].append(rtl)
            print(f"  {freq:7.0f} Hz  L→R rep {rep}: main {ltr['main_dbfs']:.2f}, "
                  f"leak {ltr['leak_dbfs']:.2f}, Xtalk {ltr['xtalk_db']:.2f} dB, coh {ltr['coherence']:.3f}")
            print(f"  {freq:7.0f} Hz  R→L rep {rep}: main {rtl['main_dbfs']:.2f}, "
                  f"leak {rtl['leak_dbfs']:.2f}, Xtalk {rtl['xtalk_db']:.2f} dB, coh {rtl['coherence']:.3f}")
        for direction in ("L->R", "R->L"):
            rows = out["frequencies"][freq_key][direction]
            summary = {}
            for field in ("main_dbfs", "leak_dbfs", "xtalk_db", "coherence"):
                values = [float(r[field]) for r in rows]
                summary[field] = {
                    "median": float(statistics.median(values)),
                    "min": float(min(values)),
                    "max": float(max(values)),
                }
            out["frequencies"][freq_key][direction + "_summary"] = summary
    return out, warnings


def parse_loopback_csv(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        level = str(int(round(float(row["generator_dbfs"]))))
        ch = row["channel"]
        result.setdefault(level, {}).setdefault(ch, []).append({
            "adc_dbfs": float(row["adc_fundamental_dbfs"]),
            "loop_gain_db": float(row["loop_gain_db"]),
            "thd_pct": float(row["thd_pct"]),
            "thdn_dbc": float(row["thdn_db"]),
            "noise_only_dbfs": float(row["noise_only_dbfs"]),
            "H2_dbc": as_float(row.get("H2_dbc")),
            "H3_dbc": as_float(row.get("H3_dbc")),
        })
    summary: dict[str, Any] = {}
    for level, channels in result.items():
        summary[level] = {}
        for ch, samples in channels.items():
            summary[level][ch] = {}
            for key in samples[0]:
                vals = [s[key] for s in samples if s[key] is not None and math.isfinite(s[key])]
                if vals:
                    summary[level][ch][key] = {
                        "median": float(statistics.median(vals)),
                        "min": float(min(vals)),
                        "max": float(max(vals)),
                    }
                else:
                    summary[level][ch][key] = {"median": None, "min": None, "max": None}
    return summary


def as_float(v: Any) -> float | None:
    if v in (None, "", "None"):
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def median_xtalk(xtalk: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for freq, directions in xtalk["frequencies"].items():
        out[freq] = {}
        for direction in ("L->R", "R->L"):
            out[freq][direction] = {
                k: v["median"] for k, v in directions[direction + "_summary"].items()
            }
    return out


def load_reference(label: str | None, out_root: Path) -> tuple[str, dict[str, Any]]:
    if not label or label == "A0":
        return "A0", BASELINE_A0
    p = out_root / label / "metrics.json"
    if not p.exists():
        raise RuntimeError(f"Не найден сохранённый эталон {label}: {p}")
    return label, json.loads(p.read_text(encoding="utf-8"))


def read_latest_label(out_root: Path) -> str:
    latest = out_root / "latest.json"
    if latest.exists():
        try:
            return json.loads(latest.read_text(encoding="utf-8"))["label"]
        except Exception:
            pass
    return "A0"


def verdict(delta: float | None, tolerance: float = 1.0, lower_is_better: bool = True) -> str:
    if delta is None or not math.isfinite(delta):
        return "нет данных"
    d = delta if lower_is_better else -delta
    if d < -tolerance:
        return "ЛУЧШЕ"
    if d > tolerance:
        return "ХУЖЕ"
    return "≈ без изм."


def fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def build_summary(label: str, ref_label: str, current: dict[str, Any], ref: dict[str, Any]) -> str:
    lines = [
        f"ValveBench · E-MU 0404 · состояние {label or 'без сохранения'}",
        f"Сравнение с: {ref_label}",
        f"Частота: {FS} Гц · формат S24_3LE · 2 канала",
        "Знак Δ: текущее минус эталон. Для шумов/искажений/xtalk отрицательная Δ обычно лучше.",
        "",
        "1) ADC-only, входы замкнуты, ЦАП не работает",
        "Полоса                 L текущее    ΔL       оценка       R текущее    ΔR       оценка",
    ]
    better = worse = stable = 0

    def add_score(v: str) -> None:
        nonlocal better, worse, stable
        if v == "ЛУЧШЕ": better += 1
        elif v == "ХУЖЕ": worse += 1
        elif v.startswith("≈"): stable += 1

    band_rows = [("full_rms", "Полный RMS", None, None)] + [(key, label_band, _lo, _hi) for key, label_band, _lo, _hi in BANDS]
    for key, label_band, _lo, _hi in band_rows:
        chunks = [label_band]
        for ch in ("L", "R"):
            if key == "full_rms":
                now = current["adc_only"]["channels"][ch]["full_rms_dbfs"]
                old = ref["adc_only"]["channels"][ch]["full_rms_dbfs"]
            else:
                now = current["adc_only"]["channels"][ch]["bands"][key]["integrated_dbfs"]
                old = ref["adc_only"]["channels"][ch]["bands"][key]
                if isinstance(old, dict): old = old["integrated_dbfs"]
            delta = now - float(old)
            v = verdict(delta)
            add_score(v)
            chunks.extend([f"{now:9.2f}", f"{delta:+7.2f}", f"{v:10s}"])
        lines.append(f"{chunks[0]:22s} {chunks[1]} {chunks[2]} {chunks[3]} {chunks[4]} {chunks[5]} {chunks[6]}")

    lines += ["", "2) Loopback DAC→ADC, уровни генератора; H2/H3 в dBc, остаток — dBFS",
              "Уровень  Канал   Δусил.   H2 сейчас  ΔH2     H3 сейчас  ΔH3     остаток   Δост.   итог"]
    for level in ("-1", "-3", "-6", "-9", "-12"):
        for ch in ("L", "R"):
            now_row = current["loopback"].get(level, {}).get(ch)
            old_row = ref["loopback"].get(level, {}).get(ch)
            if not now_row or not old_row:
                lines.append(f"{level:>6s}  {ch}     нет данных")
                continue
            def current_val(key: str) -> float | None:
                item = now_row.get(key)
                return None if item is None else (item.get("median") if isinstance(item, dict) else item)
            def old_val(key: str) -> float | None:
                item = old_row.get(key)
                return None if item is None else (item.get("median") if isinstance(item, dict) else item)
            gain_now, gain_old = current_val("loop_gain_db"), old_val("loop_gain_db")
            h2_now, h2_old = current_val("H2_dbc"), old_val("H2_dbc")
            h3_now, h3_old = current_val("H3_dbc"), old_val("H3_dbc")
            residual_now, residual_old = current_val("noise_only_dbfs"), old_val("noise_only_dbfs")
            dh2 = None if h2_now is None or h2_old is None else h2_now - h2_old
            dh3 = None if h3_now is None or h3_old is None else h3_now - h3_old
            dr = None if residual_now is None or residual_old is None else residual_now - residual_old
            dg = None if gain_now is None or gain_old is None else gain_now - gain_old
            statuses = [verdict(dh2), verdict(dh3), verdict(dr)]
            for s in statuses: add_score(s)
            # The total is neutral unless at least one metric moved materially.
            if "ХУЖЕ" in statuses and "ЛУЧШЕ" in statuses:
                total = "СМЕШАННО"
            elif "ХУЖЕ" in statuses:
                total = "ХУЖЕ"
            elif "ЛУЧШЕ" in statuses:
                total = "ЛУЧШЕ"
            else:
                total = "≈ без изм."
            lines.append(
                f"{level:>6s}  {ch}  {fmt(dg,2):>7s}  {fmt(h2_now,1):>9s} {fmt(dh2,1):>7s}  "
                f"{fmt(h3_now,1):>9s} {fmt(dh3,1):>7s}  {fmt(residual_now,1):>8s} {fmt(dr,1):>7s}  {total}"
            )

    lines += ["", "3) Межканальное проникновение при раздельном возбуждении",
              "Частота  Направл.  Xtalk сейчас  Δ к эталону   Оценка   Утечка dBFS  Coh"]
    for freq in ("100", "1000", "10000"):
        for direction in ("L->R", "R->L"):
            now = current["xtalk"].get(freq, {}).get(direction)
            old = ref["xtalk"].get(freq, {}).get(direction)
            if not now or not old:
                continue
            d = float(now["xtalk_db"]) - float(old["xtalk_db"])
            v = verdict(d)
            add_score(v)
            lines.append(
                f"{freq:>7s}  {direction:7s}  {now['xtalk_db']:11.2f}  {d:+11.2f}  "
                f"{v:8s}  {now['leak_dbfs']:11.2f}  {now['coherence']:.3f}"
            )

    lines += ["", f"Сводный счёт отдельных показателей: лучше {better} · хуже {worse} · без существенного изменения {stable}.",
              "Примечания:",
              "- Для ADC-only и loopback входы должны быть подключены в соответствии с подсказками скрипта.",
              "- Xtalk: более отрицательное значение — меньше проникновение.",
              "- H2/H3: более отрицательное dBc — меньше гармоника; оценивай её только если она выше шумового пола.",
              "- Остаток noise-only — не только белый шум: он включает все компоненты вне исключённых окон несущей и обнаруженных гармоник.",
              "- Изменение loop gain более 0,5 dB означает, что сначала надо проверить уровни/подключение, а не объявлять улучшение.",
              "- A0 восстановлен из исходных отчётов, поэтому сравнение с A0 не заменяет сохранённые исходные WAV."]
    return "\n".join(lines)


def save_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run(label: str | None, against: str | None, dev: str) -> int:
    out_root = ROOT / "emu_measurements"
    reference_label = against or read_latest_label(out_root)
    ref_label, reference = load_reference(reference_label, out_root)

    final_dir: Path | None = None
    if label:
        if not re.fullmatch(r"A\d+", label):
            raise RuntimeError("Имя состояния должно иметь вид A1, A2, A3…")
        final_dir = out_root / label
        if final_dir.exists():
            raise RuntimeError(f"Папка {final_dir} уже существует. Используй следующий номер состояния (например A2).")
        out_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=f".{label}_incomplete_", dir=out_root))
    else:
        work_dir = Path(tempfile.mkdtemp(prefix=".emu_bench_temp_", dir=ROOT))

    try:
        print("\nValveBench E-MU A/B MEASUREMENT")
        print(f"Устройство: {dev} · Fs={FS} Гц · S24_3LE · 2 канала · FFT/blocks={BLOCKS}")
        print(f"Сравнение с состоянием: {ref_label}")
        print("Никакие настройки калибровки ValveBench не изменяются.")

        ask_enter(
            "ЭТАП 1/2 — ADC ONLY\n"
            "1. Закрой GTK ValveBench и другие программы, использующие карту.\n"
            "2. Отключи оба кабеля DAC→ADC.\n"
            "3. Установи короткозамыкающие RCA-заглушки ТОЛЬКО на ДВА ВХОДА АЦП.\n"
            "4. Оба переключателя входной земли — GROUNDED INPUT.\n"
            "5. Не замыкай выходы ЦАП. Фантомное питание выключено."
        )

        cal = calib.load()
        backend = AlsaCli(dev, in_map=cal["in_map"], out_map=cal["out_map"])
        rig = Rig(backend, cal, fs=FS, blocks=BLOCKS, settle=0.25)
        rig.open()
        adc, adc_notes = measure_adc_only(rig, work_dir / "adc-only-192k.wav")
        for note in adc_notes:
            print("! ADC-only:", note)

        ask_enter(
            "ЭТАП 2/2 — LOOPBACK + CROSSTALK\n"
            "1. Убери обе RCA-заглушки со входов АЦП.\n"
            "2. Подключи DAC L → ADC L и DAC R → ADC R.\n"
            "3. Оставь оба переключателя в положении GROUNDED INPUT.\n"
            "4. Проверь, что подключены именно LINE OUT → LINE IN, не выход наушников."
        )

        print("\n[2/3] Измеряю loopback: 5 уровней × 3 повтора, шум до и после...")
        loop_csv = work_dir / "loopback.csv"
        loop_text = run_loopback_report(
            rig, frequency=997.0, levels=LOOP_LEVELS, repeats=LOOP_REPEATS,
            silence_seconds=LOOP_SILENCE_SECONDS, csv_path=str(loop_csv)
        )
        (work_dir / "loopback.txt").write_text(loop_text, encoding="utf-8")
        loop_metrics = parse_loopback_csv(loop_csv)
        validity_match = re.search(r"SERIES VALIDITY:\s*(.+)", loop_text)
        validity = validity_match.group(1).strip() if validity_match else "не удалось определить"
        print("Статус loopback:", validity)

        xtalk, xtalk_warnings = measure_xtalk(rig)
        xtalk_medians = median_xtalk(xtalk)
        xtalk_rows = []
        for freq, dirs in xtalk["frequencies"].items():
            for direction in ("L->R", "R->L"):
                for i, item in enumerate(dirs[direction], 1):
                    xtalk_rows.append({"frequency_hz": int(freq), "direction": direction, "repeat": i, **item})
        save_csv(work_dir / "xtalk-detail.csv", xtalk_rows)

        # Compact crosstalk text report with individual repeats and medians.
        xtalk_lines = ["E-MU 0404 · DETAILED CROSSTALK", f"Fs={FS} · level={XTALK_LEVEL} dBFS · repeats={XTALK_REPEATS}"]
        for freq, dirs in xtalk["frequencies"].items():
            for direction in ("L->R", "R->L"):
                xtalk_lines.append(f"\n{freq} Hz · {direction}")
                for i, item in enumerate(dirs[direction], 1):
                    xtalk_lines.append(f"  rep {i}: main={item['main_dbfs']:.2f} dBFS; leak={item['leak_dbfs']:.2f} dBFS; "
                                       f"xtalk={item['xtalk_db']:.2f} dB; coherence={item['coherence']:.3f}")
                s = dirs[direction + "_summary"]
                xtalk_lines.append(f"  median: main={s['main_dbfs']['median']:.2f}; leak={s['leak_dbfs']['median']:.2f}; "
                                   f"xtalk={s['xtalk_db']['median']:.2f} dB; coherence={s['coherence']['median']:.3f}")
        if xtalk_warnings:
            xtalk_lines.extend("WARNING: " + w for w in xtalk_warnings)
        (work_dir / "xtalk-detail.txt").write_text("\n".join(xtalk_lines), encoding="utf-8")

        current = {
            "label": label or "UNSAVED",
            "fs": FS,
            "device": dev,
            "adc_only": adc,
            "loopback": loop_metrics,
            "loopback_validity": validity,
            "xtalk": xtalk_medians,
            "xtalk_warnings": xtalk_warnings,
            "adc_notes": adc_notes,
            "reference_eligible": validity == "OK" and not xtalk_warnings and not adc_notes,
            "baseline_a0_source_note": BASELINE_A0["source_note"],
        }
        summary = build_summary(label or "без сохранения", ref_label, current, reference)
        print("\n" + "=" * 76)
        print(summary)
        if label:
            (work_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
            (work_dir / "metrics.json").write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            # Keep a copy of the full baseline used for this comparison.
            (work_dir / "reference.json").write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding="utf-8")
            # Atomic commit: incomplete/invalid runs are archived but do not replace the comparison reference.
            work_dir.rename(final_dir)
            if current["reference_eligible"]:
                (out_root / "latest.json").write_text(json.dumps({"label": label}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"\nСостояние {label} принято как новая точка сравнения.")
            else:
                print(f"\nСостояние {label} сохранено, но НЕ назначено эталоном: проверь validity/ALSA warnings.")
            print(f"ПОЛНЫЙ РЕЗУЛЬТАТ СОХРАНЁН: {final_dir}")
            print("Внутри: adc-only-192k.wav, adc-only-spectrum.csv, loopback.txt/.csv, xtalk-detail.txt/.csv, metrics.json, summary.txt, reference.json")
        else:
            print("\nРезультаты не сохранены. Для архивирования этого состояния укажи метку A1, A2…")
        return 0
    finally:
        # Remove temporary or incomplete data unless atomically promoted to final_dir.
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Один запуск для контрольных измерений E-MU 0404 до/после аппаратной модификации."
    )
    parser.add_argument("label", nargs="?", help="метка состояния A1, A2…; без метки результаты не сохраняются")
    parser.add_argument("--against", help="с чем сравнивать: A0 или ранее сохранённое состояние A1/A2…")
    parser.add_argument("--dev", default="hw:2,0", help="ALSA device (default: hw:2,0)")
    args = parser.parse_args()
    try:
        return run(args.label, args.against, args.dev)
    except KeyboardInterrupt:
        print("\nПрервано пользователем; незавершённые файлы удалены.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nОШИБКА: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Проверь подключения и каталог проекта. Незавершённый прогон не станет эталоном.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
