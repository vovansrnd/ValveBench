#!/usr/bin/env python3
"""v2.18 п.2 — Монте-Карло на синтетических сигналах с ИЗВЕСТНЫМ ppm.

Оценщики: production-центроид (dsp.analyze_tone), LSQ (нелинейный фит),
фазовый наклон, joint_naive (вес 1/σ²), joint_gls (полная ковариация).
Уровни −72/−66/−60/−42/−30 dBFS (как в v2.14), шумовые условия:
  white_indep  — белый шум, независимые L/R (пол L −80, R −96 дБFS);
  white_corr   — общий шум с коэффициентом передачи в R −16 дБ + малые
                 независимые добавки (ρ ≈ 0.97 — как утверждает аудитор);
  real_noise   — реальные шумовые сегменты из тихих зон захватов v2.14
                 (сохраняют корреляцию и окраску реального тракта).
Плюс: помеха 1.02 кГц на −20 дБ относительно тона (отдельная серия).

Метрики: bias, σ, max|err| относительно известного ppm; сравнение σ_pred
(GLS, профильный метод) с фактической σ; аналитический CRLB для белого шума.
Seed каждого прогона документирован. Playback НЕТ.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from valvebench import calib, dsp  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v218_estimators_real import est_lsq, est_phase_slope, est_joint, ppm_of  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 192000
DUR = 2.0
TRIALS = 50
LEVELS = [-72.0, -66.0, -60.0, -42.0, -30.0]
PPM_TRUE = -23.5
BASE_SEED = 20261114
NOISE_L_DBFS = -80.0     # пол L реального тракта
NOISE_R_DBFS = -96.0     # пол R
RHO_TARGET = 0.97
SRC = Path("/opt/MyApps/Adudio/ValveBench-v214-clock-diagnostic/"
           "measurements/diagnostics/v214-clock-diagnostic")


def synth_tone(fs, n, ppm, level_dbfs, phase0=0.0):
    t = np.arange(n) / fs
    f = 1000.0 * (1.0 + ppm * 1e-6)
    return dsp.dbfs_to_amp(level_dbfs) * np.sin(2 * np.pi * f * t + phase0)


def white_pair(n, seed):
    rng = np.random.default_rng(seed)
    nL = dsp.dbfs_to_amp(NOISE_L_DBFS) * rng.standard_normal(n)
    nR = dsp.dbfs_to_amp(NOISE_R_DBFS) * rng.standard_normal(n)
    return nL, nR


def white_corr_pair(n, seed):
    """Общий шум: в L с усилением 1.0, в R с −16 дБ; независимые добавки
    подобраны так, чтобы полы соответствовали реальным, ρ ≈ 0.97."""
    rng = np.random.default_rng(seed)
    g = 10 ** (-16.0 / 20.0)                       # 0.158
    # хотим: var(L) = σc² + σiL² = vL; var(R) = σc²g² + σiR² = vR;
    # ρ = σc²·g / sqrt(vL·vR) = 0.97
    vL = dsp.dbfs_to_amp(NOISE_L_DBFS) ** 2
    vR = dsp.dbfs_to_amp(NOISE_R_DBFS) ** 2
    # ρ = 0.97 → σc² = 0.97·sqrt(vL·vR)/g
    sc2 = RHO_TARGET * math.sqrt(vL * vR) / g
    siL2 = max(vL - sc2, 1e-30)
    siR2 = max(vR - sc2 * g * g, 1e-30)
    common = math.sqrt(sc2) * rng.standard_normal(n)
    nL = common + math.sqrt(siL2) * rng.standard_normal(n)
    nR = g * common + math.sqrt(siR2) * rng.standard_normal(n)
    return nL, nR


def load_real_noise_pairs():
    """Тихие сегменты реальных захватов: пары (L, R) по 2 с из pre-roll
    (до onset−0.3 с) — сохраняют корреляцию и окраску реального тракта."""
    pairs = []
    caps = {2: "20261114-004425-clock-02--66dB-capture.wav",
            15: "20261114-004425-clock-15--66dB-capture.wav",
            16: "20261114-004425-clock-16--72dB-capture.wav",
            3: "20261114-004425-clock-03--60dB-capture.wav",
            4: "20261114-004425-clock-04--54dB-capture.wav",
            8: "20261114-004425-clock-08--30dB-capture.wav"}
    onsets = {2: 1.855, 15: 1.845, 16: 1.855, 3: 1.85, 4: 1.855, 8: 1.86}
    n = int(DUR * FS)
    for idx, name in caps.items():
        _, cap = wavfile.read(str(SRC / name))
        cap = cap.astype(np.float64)
        start = int((onsets[idx] + 2.8) * FS)   # после тона + запас
        if start + n <= len(cap):
            pairs.append(cap[start:start + n, :])
    return pairs


def est_prod_centroid(seg, cal):
    r = dsp.analyze_tone(seg, FS, 1000.0, N, nharm=2,
                         band=(20.0, cal["band_hi"]), mains=cal["mains"])
    return (r["f0"] / 1000.0 - 1) * 1e6


def run_cell(cond, level, trials, cal, real_pairs, interferer, cell_no):
    errs = {"prod_L": [], "prod_R": [], "lsq_L": [], "lsq_R": [],
            "phase_L": [], "phase_R": [], "joint_naive": [], "joint_gls": [],
            "sigma_pred": []}
    for tr in range(trials):
        seed = BASE_SEED + cell_no * 10000 + tr          # детерминированный seed
        phase0 = 2 * math.pi * ((seed * 37) % 1000) / 1000
        toneL = synth_tone(FS, int(DUR * FS), PPM_TRUE, level, phase0)
        toneR = synth_tone(FS, int(DUR * FS), PPM_TRUE, level, phase0 + 0.7)
        if cond == "white_indep":
            nL, nR = white_pair(int(DUR * FS), seed)
        elif cond == "white_corr":
            nL, nR = white_corr_pair(int(DUR * FS), seed)
        else:                                     # real_noise
            seg = real_pairs[tr % len(real_pairs)]
            start = seed % max(1, len(seg) - int(DUR * FS))
            nL = seg[start:start + int(DUR * FS), 0].copy()
            nR = seg[start:start + int(DUR * FS), 1].copy()
        if interferer:
            t = np.arange(int(DUR * FS)) / FS
            int_amp = dsp.dbfs_to_amp(level) * 10 ** (-20.0 / 20.0)
            nL += int_amp * np.sin(2 * np.pi * 1020.0 * t + 1.0)
            nR += int_amp * np.sin(2 * np.pi * 1020.0 * t + 2.0)
        L = toneL + nL
        R = toneR + nR
        errs["prod_L"].append(est_prod_centroid(L, cal))
        errs["prod_R"].append(est_prod_centroid(R, cal))
        # 48 кГц для быстрых оценщиков: частота 1 кГц не затрагивается,
        # ковариация шума сохраняется; production-центроид — на полном 192 кГц
        L48 = resample_poly(L, 1, 4)
        R48 = resample_poly(R, 1, 4)
        errs["lsq_L"].append(ppm_of(est_lsq(L48, 48000.0, 1000.0, 48000)))
        errs["lsq_R"].append(ppm_of(est_lsq(R48, 48000.0, 1000.0, 48000)))
        f_phL, _ = est_phase_slope(L48, 48000.0, 1000.0)
        f_phR, _ = est_phase_slope(R48, 48000.0, 1000.0)
        errs["phase_L"].append(ppm_of(f_phL) if f_phL is not None else None)
        errs["phase_R"].append(ppm_of(f_phR) if f_phR is not None else None)
        jn = est_joint(np.column_stack([L48, R48]), 48000.0, 1000.0, "naive")
        jg = est_joint(np.column_stack([L48, R48]), 48000.0, 1000.0, "gls")
        errs["joint_naive"].append(ppm_of(jn["f"]))
        errs["joint_gls"].append(ppm_of(jg["f"]))
        errs["sigma_pred"].append(jg["var_f"] ** 0.5 / 1000 * 1e6
                                  if jg["var_f"] else None)
    return errs


def stats(v):
    a = np.asarray([x for x in v if x is not None], dtype=np.float64)
    if not len(a):
        return None
    return {"bias": round(float(np.mean(a) - PPM_TRUE), 3),
            "sigma": round(float(np.std(a)), 3),
            "max_abs": round(float(np.max(np.abs(a - PPM_TRUE))), 3),
            "n_valid": int(len(a))}


def crlb_dbfs(level_dbfs):
    a = dsp.dbfs_to_amp(level_dbfs)
    sigma = dsp.dbfs_to_amp(NOISE_L_DBFS)
    n = int(DUR * FS)
    t_total = DUR
    return math.sqrt(6 * sigma ** 2 / (a ** 2 * n * t_total ** 2)) / (2 * np.pi) / 1000 * 1e6


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    real_pairs = load_real_noise_pairs()
    print(f"реальных шумовых пар: {len(real_pairs)}", flush=True)
    summary = []
    cell_no = 0
    for cond in ("real_noise", "white_corr", "white_indep"):
        for level in LEVELS:
            errs = run_cell(cond, level, TRIALS, cal, real_pairs, False, cell_no)
            cell_no += 1
            row = {"condition": cond, "level_dbfs": level,
                   "crlb_ppm": round(crlb_dbfs(level), 3) if level <= NOISE_L_DBFS + 6 else None}
            for k in errs:
                row[k] = stats(errs[k])
            summary.append(row)
            print(f"{cond:12s} {level:+6.0f}: prod L σ={row['prod_L']['sigma']:6.2f} "
                  f"R σ={row['prod_R']['sigma']:6.2f} | lsq L σ={row['lsq_L']['sigma']:6.2f} "
                  f"R σ={row['lsq_R']['sigma']:6.2f} | joint_gls σ={row['joint_gls']['sigma']:6.2f} "
                  f"(σ_pred {row['sigma_pred']['sigma'] if row['sigma_pred'] else '—'}; "
                  f"CRLB {row['crlb_ppm']})", flush=True)
    # помеха: одна серия на −72
    errs = run_cell("white_corr", -72.0, TRIALS, cal, real_pairs, True, cell_no)
    row = {"condition": "white_corr+interference", "level_dbfs": -72.0,
           "crlb_ppm": None}
    for k in errs:
        row[k] = stats(errs[k])
    summary.append(row)
    print(f"{'+interf':12s}  -72: prod L sigma={row['prod_L']['sigma']} | "
          f"lsq L sigma={row['lsq_L']['sigma']} | joint_gls sigma={row['joint_gls']['sigma']}",
          flush=True)
    json.dump({"ppm_true": PPM_TRUE, "trials_per_cell": TRIALS,
               "base_seed": BASE_SEED, "cells": summary},
              open(OUT / "v218-synth-montecarlo.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v218-synth-montecarlo.json")


if __name__ == "__main__":
    main()
