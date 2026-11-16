#!/usr/bin/env python3
"""v2.18.5 — воспроизведение «868 Гц» + отказ ПРАВЛЕННОГО оценщика.

Ревизия v2.18.4 п.2: «Re-run the exact 868 reproduction» на line-aware
оценщике. Две части:
  1. ЛЕГАСИ: ТОЧНАЯ копия est_lsq из 8d8b040 (v2.18.1) — воспроизведение
     механизма ( coarse-пик на границе полосы + возрастающие соседи +
     невалидная выпуклая парабола + неограниченное смещение );
  2. ПРАВЛЕННЫЙ: v2185_estimators.est_lsq на ТЕХ ЖЕ сигналах (точные
     seed 218200 + 997·1000 + 300 + tr) — ожидается edge_peak 100/100
     (boundary-guard: coarse-argmax на границе интервала поиска),
     и est_joint — fail-closed (None, None, fs, 'edge_peak').

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench import dsp  # noqa: E402
from v2185_estimators import est_joint, est_lsq  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 192000
PILOT = 997.0
PPM_TRUE = -23.5
MAINS_LEVEL = -30.0
BASE_SEED = 218200          # формула v2181_lsq_pilots.py (v2.18.1)


def est_lsq_8d8b040(seg, fs, f_nom, coarse_hz=2.0):
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
        D = np.column_stack([np.sin(2 * math.pi * f * t),
                             np.cos(2 * math.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    f_best = grid[i]
    log["rss_argmin_hz"] = round(f_best, 4)
    if 0 < i < len(grid) - 1:
        denom2 = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom2 > 0:
            f_best += (rss[i - 1] - rss[i + 1]) / (2 * denom2) * \
                (grid[1] - grid[0])
    log["f_est_hz"] = round(f_best, 4)
    return f_best, log


def make_trial(tr):
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
    return x, seed


def main():
    legacy_logs, repaired = [], []
    joint_reject = 0
    for tr in range(100):
        x, seed = make_trial(tr)
        f_leg, log = est_lsq_8d8b040(x, FS, PILOT)
        log["trial"] = tr
        log["seed"] = seed
        log["f_est_hz"] = round(f_leg, 4)
        legacy_logs.append(log)
        # ПРАВЛЕННЫЙ оценщик на том же сигнале
        qc = est_lsq(x, FS, PILOT)
        jg = est_joint(np.column_stack([x, x]), FS, PILOT)
        if qc["status"] == "edge_peak" and "f_est" not in qc:
            repaired.append({"trial": tr, "status": qc["status"],
                             "coarse_peak_hz": qc.get("coarse_peak_hz")})
        if jg[0] is None and jg[3] == "edge_peak":
            joint_reject += 1
        if tr % 20 == 0:
            print(f"  trial {tr}: легаси f_est={f_leg:.2f} Гц | "
                  f"v2185 статус={qc['status']}", flush=True)

    f_all = [l["f_est_hz"] for l in legacy_logs]
    n_868 = sum(1 for f in f_all if abs(f - 868.0) < 5.0)
    n_invalid = sum(1 for l in legacy_logs
                    if l.get("coarse_curvature_sign", "").startswith("ВЫПУКЛ"))
    n_fly = sum(1 for l in legacy_logs
                if abs(l.get("coarse_offset_bins") or 0) > 0.5)
    ex = next((l for l in legacy_logs if abs(l["f_est_hz"] - 868.0) < 5.0),
              None)
    ex_rep = repaired[0] if repaired else None
    summary = {
        "scenario": "пилот 997 Гц (−72 дБFS) + гребёнка 50·k −30 дБFS "
                    "(40 гармоник) + белый −96 дБFS; N=192000 (1 c, 192 кГц)",
        "seed_formula": "218200 + 997000 + 300 + tr (tr 0..99)",
        "legacy": {
            "version": "est_lsq из 8d8b040 (v2.18.1): coarse-парабола без "
                       "проверки кривизны и без ограничения смещения",
            "trials": 100,
            "trials_near_868hz": n_868,
            "trials_invalid_coarse_parabola": n_invalid,
            "trials_coarse_offset_gt_half_bin": n_fly,
            "f_est_min_hz": round(min(f_all), 3),
            "f_est_max_hz": round(max(f_all), 3),
            "example_trial": ex,
        },
        "repaired_v2185": {
            "version": "v2185_estimators.est_lsq: boundary-guard + вогнутая "
                       "парабола ±0.5 бина + line-QC",
            "trials": 100,
            "trials_edge_peak": len(repaired),
            "example": ex_rep,
        },
        "joint_fail_closed": {
            "trials": 100,
            "rejected_with_edge_peak": joint_reject,
            "semantics": "(None, None, fs, 'edge_peak') — невалидный старт "
                         "не потребляется (ревизия v2.18.4 п.3)",
        },
        "verdict": None,
    }
    if n_868 == 100 and len(repaired) == 100 and joint_reject == 100:
        summary["verdict"] = (
            "МЕХАНИЗМ ВОСПРОИЗВЕДЁН 100/100 (легаси), ПРАВЛЕННЫЙ оценщик "
            "ОТКЛОНЯЕТ 100/100 (edge_peak), joint fail-closed 100/100")
    else:
        summary["verdict"] = (
            f"РАСХОЖДЕНИЕ: легаси 868 {n_868}/100, edge_peak "
            f"{len(repaired)}/100, joint отказ {joint_reject}/100")
    print(json.dumps(summary, ensure_ascii=False, indent=1)[:900], flush=True)
    json.dump({"summary": summary, "legacy_per_trial": legacy_logs,
               "repaired_per_trial": repaired},
              open(OUT / "v2185-868-reproduction.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2185-868-reproduction.json")


if __name__ == "__main__":
    main()
