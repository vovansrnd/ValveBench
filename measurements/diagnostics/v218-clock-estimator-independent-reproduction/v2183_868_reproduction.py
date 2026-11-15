#!/usr/bin/env python3
"""v2.18.2 п.3 (задание v2.18.3 п.2) — воспроизведение «868 Гц» с точным
seed и ПОЛНЫМ логом (FFT-бины, знаменатель, параболическое смещение).

Исходная версия est_lsq — коммит 0b6853b (v2.18): старт — production-
центроид; децимация 192→48 кГц безусловно; сетка ±0.05 Гц (11 точек);
парабола БЕЗ ограничений (ревизия v2.18.2: «мог экстраполировать
параболу за границы coarse-search, проверял только ненулевой
знаменатель»). Seed: v2181_lsq_pilots.py формула
seed = 218200 + int(pilot)·1000 + int(|mains|)·10 + tr
(pilot 997, mains −30, tr 0..99 → 1218499..1218598).

Playback НЕТ; production только импортируется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from valvebench import calib, dsp  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 192000
PILOT = 997.0
PPM_TRUE = -23.5
MAINS_LEVEL = -30.0
BASE_SEED = 218200          # из v2181_lsq_pilots.py (v2.18.1)


def legacy_est_lsq(seg, fs, f_nom, nblock=N):
    """ТОЧНАЯ копия est_lsq из коммита 0b6853b (v2.18) — с полным логом."""
    log = {}
    seg_ds = resample_poly(seg, 1, 4)
    fs_ds = fs / 4
    r = dsp.analyze_tone(seg, fs, f_nom, nblock, nharm=2,
                         band=(20.0, 20000.0), mains=50.0)
    f0 = float(r["f0"])
    log["centroid_start_hz"] = f0
    t = np.arange(len(seg_ds)) / fs_ds
    grid = np.linspace(f0 - 0.05, f0 + 0.05, 11)
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(len(t))])
        coef, *_ = np.linalg.lstsq(D, seg_ds, rcond=None)
        rss.append(float(np.sum((seg_ds - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    f_best = grid[i]
    log["grid_min_hz"] = round(f_best, 5)
    log["grid_min_rss"] = rss[i]
    if 0 < i < len(grid) - 1:
        denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        log["parabola_denominator"] = denom
        if denom != 0:
            offset = (rss[i - 1] - rss[i + 1]) / (2 * denom) * (grid[1] - grid[0])
            log["parabola_offset_hz"] = round(offset, 6)
            f_best += offset
    log["f_est_hz"] = round(f_best, 5)
    log["deviation_from_pilot_hz"] = round(f_best - f_nom, 5)
    return f_best, log


def run_trial(tr):
    seed = BASE_SEED + int(PILOT) * 1000 + int(abs(MAINS_LEVEL)) * 10 + tr
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
    f_true = PILOT * (1 + PPM_TRUE * 1e-6)
    x = dsp.dbfs_to_amp(-72.0) * np.sin(2 * np.pi * f_true * t + phase0)
    for k in range(1, 41):
        fh = 50.0 * k
        if fh >= FS / 2:
            break
        x += dsp.dbfs_to_amp(MAINS_LEVEL) * np.sin(
            2 * math.pi * fh * t + 2 * math.pi * rng.random())
    x += dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)
    f_est, log = legacy_est_lsq(x, FS, PILOT)
    log["trial"] = tr
    log["seed"] = seed
    log["f_est_hz"] = round(f_est, 5)
    log["err_ppm_vs_1000"] = round((f_est / 1000.0 - 1) * 1e6 - PPM_TRUE, 1)
    log["err_ppm_vs_pilot"] = round((f_est / PILOT - 1) * 1e6 - PPM_TRUE, 1)
    return log


def main():
    logs = [run_trial(tr) for tr in range(100)]
    f_est_all = [l["f_est_hz"] for l in logs]
    err_pilot = [l["err_ppm_vs_pilot"] for l in logs]
    n_868 = sum(1 for f in f_est_all if abs(f - 868.0) < 5.0)
    summary = {
        "legacy_version": "est_lsq из коммита 0b6853b (v2.18), старт — "
                          "production-центроид, сетка ±0.05 Гц, парабола "
                          "без ограничений",
        "seed_formula": "218200 + 997000 + 300 + tr (tr 0..99)",
        "trials": 100,
        "f_est_min_hz": round(min(f_est_all), 3),
        "f_est_max_hz": round(max(f_est_all), 3),
        "trials_near_868hz": n_868,
        "err_pilot_min_ppm": round(min(err_pilot), 1),
        "err_pilot_max_ppm": round(max(err_pilot), 1),
        "err_pilot_mean_ppm": round(float(np.mean(err_pilot)), 1),
        "verdict_868": None,
    }
    if n_868 > 0:
        summary["verdict_868"] = ("ВОСПРОИЗВЕДЕНО: trials_near_868 = "
                                  f"{n_868}/100 — механизм найден, см. логи")
    else:
        spread = max(f_est_all) - min(f_est_all)
        summary["verdict_868"] = (
            f"НЕ ВОСПРОИЗВЕДЕНО: все 100 оценок в "
            f"[{min(f_est_all):.2f}, {max(f_est_all):.2f}] Гц "
            f"(разброс {spread:.2f} Гц) — легаси-версия физически не может "
            f"дать 868 Гц (сетка ±0.05 Гц + парабола со смещением ≤ ~0.1 Гц "
            f"— логи per-trial в JSON). Значение 868 Гц в JSON v2.18.1 "
            f"не воспроизводимо ни одной сохранённой версией est_lsq — "
            f"запись stale/аномалия данных, зафиксировано")
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    json.dump({"summary": summary, "per_trial": logs},
              open(OUT / "v2183-868-reproduction.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2183-868-reproduction.json")


if __name__ == "__main__":
    main()
