#!/usr/bin/env python3
"""v2.18.4 п.1 — воспроизведение «868 Гц» на ПРАВИЛЬНОЙ версии оценщика.

Ревизия v2.18.3: агент v2.18.3 воспроизвёл легаси-версию из 0b6853b, но
868 Гц был получен НЕЗАВИСИМЫМ оценщиком из v2.18.1 (коммит 8d8b040):
coarse-поиск ±2 Гц с параболическим уточнением БЕЗ ограничения смещения и
БЕЗ проверки кривизны (только denom != 0). При почти плоской окрестности
пика (denom → 0) смещение (y0−y2)/(2·denom) экстраполирует неограниченно.

Здесь: ТОЧНАЯ копия est_lsq из 8d8b040 (v2181 ветка), точные seed
(218200 + 997000 + 300 + tr, tr 0..99 — формула v2181_lsq_pilots.py),
полный лог per-trial: FFT-бины, знаменатель, смещение, f0, f_est.
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
BASE_SEED = 218200          # формула v2181_lsq_pilots.py (v2.18.1)


def est_lsq_8d8b040(seg, fs, f_nom, nblock=None, coarse_hz=2.0):
    """ТОЧНАЯ копия est_lsq из коммита 8d8b040 (ветка v2.18.1) — с полным
    логом coarse-параболы (бин, знаменатель, смещение)."""
    log = {}
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    band = (freqs >= f_nom - coarse_hz) & (freqs <= f_nom + coarse_hz)
    idx_band = np.where(band)[0]
    k = int(idx_band[int(np.argmax(X[idx_band]))])
    log["coarse_peak_bin"] = k
    log["coarse_peak_hz"] = round(k * fs / n, 3)
    if 0 < k < len(X) - 1:
        y0, y1, y2 = X[k - 1], X[k], X[k + 1]
        denom = y0 - 2 * y1 + y2
        log["coarse_neighbors"] = [round(y0, 4), round(y1, 4), round(y2, 4)]
        log["coarse_denominator"] = denom
        log["coarse_curvature_sign"] = ("вогнутый пик" if denom < 0 else
                                        "ВЫПУКЛЫЙ/ПЛОСКИЙ — парабола невалидна")
        if denom != 0:                        # ЕДИНСТВЕННАЯ проверка в v2.18.1
            offset = (y0 - y2) / (2 * denom)  # БЕЗ ограничения кривизны/масштаба
            log["coarse_offset_bins"] = round(offset, 4)
            k = k + offset                    # неограниченное смещение!
        else:
            log["coarse_offset_bins"] = None
    else:
        log["coarse_offset_bins"] = None
    f0 = k * fs / n
    log["f0_after_coarse_hz"] = round(f0, 3)
    grid = np.linspace(f0 - 0.15, f0 + 0.15, 25)
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    f_best = grid[i]
    log["rss_argmin_hz"] = round(f_best, 4)
    if 0 < i < len(grid) - 1:
        denom2 = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom2 > 0:
            f_best += (rss[i - 1] - rss[i + 1]) / (2 * denom2) * (grid[1] - grid[0])
    log["f_est_hz"] = round(f_best, 4)
    return f_best, log


def run_trial(tr):
    seed = BASE_SEED + int(PILOT) * 1000 + int(abs(MAINS_LEVEL)) * 10 + tr
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
    f_true = PILOT * (1 + PPM_TRUE * 1e-6)
    x = dsp.dbfs_to_amp(-72.0) * np.sin(2 * math.pi * f_true * t + phase0)
    for k in range(1, 41):
        fh = 50.0 * k
        if fh >= FS / 2:
            break
        x += dsp.dbfs_to_amp(MAINS_LEVEL) * np.sin(
            2 * math.pi * fh * t + 2 * math.pi * rng.random())
    x += dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)
    f_est, log = est_lsq_8d8b040(x, FS, PILOT)
    log["trial"] = tr
    log["seed"] = seed
    log["f_est_hz"] = round(f_est, 4)
    log["err_ppm_vs_1000"] = round((f_est / 1000.0 - 1) * 1e6 - PPM_TRUE, 1)
    return log


def main():
    logs = [run_trial(tr) for tr in range(100)]
    f_all = [l["f_est_hz"] for l in logs]
    n_868 = sum(1 for f in f_all if abs(f - 868.0) < 5.0)
    # сколько прогонов имели невалидную (плоскую/выпуклую) coarse-параболу
    n_invalid = sum(1 for l in logs
                    if l.get("coarse_curvature_sign", "").startswith("ВЫПУКЛ"))
    n_fly = sum(1 for l in logs
                if abs(l.get("coarse_offset_bins") or 0) > 0.5)
    out_of_band = sum(1 for f in f_all if abs(f - PILOT) > 2.15)
    summary = {
        "version": "est_lsq из коммита 8d8b040 (ветка v2.18.1) — независимый "
                   "старт, coarse-парабола БЕЗ ограничения смещения и БЕЗ "
                   "проверки кривизны (только denom != 0)",
        "seed_formula": "218200 + 997000 + 300 + tr (tr 0..99) — формула "
                        "v2181_lsq_pilots.py",
        "trials": 100,
        "f_est_min_hz": round(min(f_all), 3),
        "f_est_max_hz": round(max(f_all), 3),
        "trials_near_868hz": n_868,
        "trials_invalid_coarse_parabola": n_invalid,
        "trials_coarse_offset_gt_half_bin": n_fly,
        "trials_f_est_out_of_search_band": out_of_band,
        "verdict": None,
    }
    if n_868 > 0:
        ex = next(l for l in logs if abs(l["f_est_hz"] - 868.0) < 5.0)
        summary["verdict"] = ("ВОСПРОИЗВЕДЕНО: 868 Гц возникает на "
                              f"{n_868}/100 прогонов; пример trial "
                              f"{ex['trial']}: coarse-пик "
                              f"{ex['coarse_peak_hz']} Гц, знаменатель "
                              f"{ex['coarse_denominator']}, смещение "
                              f"{ex['coarse_offset_bins']} бинов → f0 "
                              f"{ex['f0_after_coarse_hz']} Гц")
        summary["example_868_trial"] = ex
    else:
        summary["verdict"] = (f"868 Гц не воспроизведён (0/100); диапазон "
                              f"f_est [{min(f_all)}, {max(f_all)}] Гц; "
                              f"невалидных парабол {n_invalid}, смещений "
                              f">0.5 бина {n_fly}, вне полосы {out_of_band}")
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    json.dump({"summary": summary, "per_trial": logs},
              open(OUT / "v2184-868-reproduction.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2184-868-reproduction.json")


if __name__ == "__main__":
    main()
