#!/usr/bin/env python3
"""v2.18 п.3 — корреляция шумов L/R: PSD, комплексная когерентность,
корреляция ошибок оценщиков.

Окна тишины: 0.25-с окна с широкополосным RMS ниже (25-й перцентиль RMS
захвата + 3 дБ) — независимая выборка с документированным критерием
(точный список «42 окон аудитора» недоступен — вложение с отчётом аудитора
не передано; это отмечено в отчёте как ограничение).

Проверяемые утверждения аудитора: ~97% когерентность шумовой составляющей
L/R; разница уровней ~16 дБ; корреляция ошибок оценщиков L/R.
Playback НЕТ; production только импортируется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import welch, csd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench import calib, dsp  # noqa: E402
from v218_estimators_real import est_lsq, ppm_of  # noqa: E402

SRC = Path("/opt/MyApps/Adudio/ValveBench-v214-clock-diagnostic/"
           "measurements/diagnostics/v214-clock-diagnostic")
OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
IDX = [1, 2, 15, 16, 3, 4, 8]
CAPTURES = {
    1: "20261114-004425-clock-01--72dB-capture.wav",
    2: "20261114-004425-clock-02--66dB-capture.wav",
    15: "20261114-004425-clock-15--66dB-capture.wav",
    16: "20261114-004425-clock-16--72dB-capture.wav",
    3: "20261114-004425-clock-03--60dB-capture.wav",
    4: "20261114-004425-clock-04--54dB-capture.wav",
    8: "20261114-004425-clock-08--30dB-capture.wav",
}
R_ONSET = {1: 2.25, 2: 1.855, 15: 1.845, 16: 1.855,
           3: 1.85, 4: 1.855, 8: 1.86}


def quiet_windows(cap):
    """0.25-с окна тишины: RMS ниже 25-го перцентиля + 3 дБ."""
    w = int(0.05 * FS)
    m = len(cap) // w * w
    rms = np.sqrt(np.mean(cap[:m].reshape(-1, w, 2) ** 2, axis=1)).max(axis=1) * math.sqrt(2)
    thr = float(np.percentile(rms, 25)) * 10 ** (3.0 / 20.0)
    quiet = rms < thr
    # непрерывные участки тишины ≥ 0.35 с
    out = []
    i = 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            dur = (j - i) * 0.05
            if dur >= 0.35:
                n_win = int(dur / 0.25)
                for k in range(n_win):
                    s = int((i * 0.05 + k * 0.25) * FS)
                    out.append((s, s + int(0.25 * FS)))
            i = j
        else:
            i += 1
    return out


def coherence_analysis():
    """Ансамблевая когерентность: усреднение кросс-спектров по окнам тишины."""
    nper = 8192
    S_LL = S_RR = S_LR = None
    n_win_total = 0
    per_capture = []
    for idx in IDX:
        _, cap = wavfile.read(str(SRC / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        wins = quiet_windows(cap)
        n_win_total += len(wins)
        sLL = sRR = sLR = None
        for (a, b) in wins:
            f, P_LL = welch(cap[a:b, 0], FS, nperseg=nper)
            _, P_RR = welch(cap[a:b, 1], FS, nperseg=nper)
            _, P_LR = csd(cap[a:b, 0], cap[a:b, 1], FS, nperseg=nper)
            sLL = P_LL if sLL is None else sLL + P_LL
            sRR = P_RR if sRR is None else sRR + P_RR
            sLR = P_LR if sLR is None else sLR + P_LR
        if sLL is None or len(wins) == 0:
            per_capture.append({"index": idx, "windows": 0})
            continue
        sLL /= len(wins); sRR /= len(wins); sLR /= len(wins)
        band = (f >= 900) & (f <= 1100)
        g2 = float(np.mean(np.abs(sLR[band]) ** 2 / (sLL[band] * sRR[band])))
        diff_db = float(np.mean(10 * np.log10(sLL[band] / sRR[band])))
        per_capture.append({"index": idx, "windows": len(wins),
                            "coherence_1kHz": round(g2, 4),
                            "psd_diff_L_minus_R_db": round(diff_db, 1)})
        if S_LL is None:
            S_LL, S_RR, S_LR = sLL * len(wins), sRR * len(wins), sLR * len(wins)
        else:
            S_LL += sLL * len(wins); S_RR += sRR * len(wins); S_LR += sLR * len(wins)
        print(f"  idx {idx:2d}: окон тишины {len(wins)}, "
              f"γ²(1кГц)={g2:.4f}, PSD L−R={diff_db:+.1f} дБ", flush=True)
    band = (f >= 900) & (f <= 1100)
    S_LL /= n_win_total; S_RR /= n_win_total; S_LR /= n_win_total
    g2_all = float(np.mean(np.abs(S_LR[band]) ** 2 / (S_LL[band] * S_RR[band])))
    diff_all = float(np.mean(10 * np.log10(S_LL[band] / S_RR[band])))
    print(f"ИТОГО: окон {n_win_total}, γ²(1кГц)={g2_all:.4f}, "
          f"PSD L−R={diff_all:+.1f} дБ", flush=True)
    return {"windows_total": n_win_total, "coherence_1kHz_all": round(g2_all, 4),
            "psd_diff_db_all": round(diff_all, 1), "per_capture": per_capture}


def estimator_error_correlation():
    """Корреляция ошибок оценщиков L/R: реальные шумовые пары + синтетический
    тон −72 dBFS (худший случай), 200 прогонов с seed."""
    cal = calib.Calibration()
    caps = {2: "20261114-004425-clock-02--66dB-capture.wav",
            15: "20261114-004425-clock-15--66dB-capture.wav",
            16: "20261114-004425-clock-16--72dB-capture.wav",
            3: "20261114-004425-clock-03--60dB-capture.wav",
            4: "20261114-004425-clock-04--54dB-capture.wav",
            8: "20261114-004425-clock-08--30dB-capture.wav"}
    onsets = {2: 1.855, 15: 1.845, 16: 1.855, 3: 1.85, 4: 1.855, 8: 1.86}
    n = int(2.0 * FS)
    truth_ppm = -23.5
    seed = 218001
    eL_prod, eR_prod, eL_lsq, eR_lsq = [], [], [], []
    for tr in range(200):
        idx = list(caps)[tr % len(caps)]
        _, cap = wavfile.read(str(SRC / caps[idx]))
        cap = cap.astype(np.float64)
        start = int((onsets[idx] + 2.8) * FS) + (seed + tr * 7919) % 60000
        nL = cap[start:start + n, 0].copy()
        nR = cap[start:start + n, 1].copy()
        t = np.arange(n) / FS
        tone = dsp.dbfs_to_amp(-72.0) * np.sin(
            2 * np.pi * 1000.0 * (1 + truth_ppm * 1e-6) * t + 0.3)
        L = tone + nL
        R = tone + nR
        for c, seg, eprod, elsq in ((0, L, eL_prod, eL_lsq), (1, R, eR_prod, eR_lsq)):
            r = dsp.analyze_tone(seg, FS, 1000.0, FS, nharm=2,
                                 band=(20.0, cal["band_hi"]), mains=cal["mains"])
            eprod.append((r["f0"] / 1000.0 - 1) * 1e6 - truth_ppm)
            elsq.append(ppm_of(est_lsq(seg, FS, 1000.0, FS)) - truth_ppm)
    def r2(a, b):
        a, b = np.asarray(a), np.asarray(b)
        return round(float(np.corrcoef(a, b)[0, 1]), 3)
    out = {"trials": 200, "level_dbfs": -72.0,
           "sigma_err_prod_L": round(float(np.std(eL_prod)), 3),
           "sigma_err_prod_R": round(float(np.std(eR_prod)), 3),
           "sigma_err_lsq_L": round(float(np.std(eL_lsq)), 3),
           "sigma_err_lsq_R": round(float(np.std(eR_lsq)), 3),
           "corr_err_prod": r2(eL_prod, eR_prod),
           "corr_err_lsq": r2(eL_lsq, eR_lsq),
           "seed_base": seed}
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    coh = coherence_analysis()
    corr = estimator_error_correlation()
    json.dump({"noise_coherence": coh, "estimator_error_correlation": corr,
               "note": ("точный список 42 окон аудитора недоступен (вложение "
                        "не передано); окна тишины отобраны по документированному "
                        "критерию RMS < P25+3 дБ, участки ≥ 0.35 с")},
              open(OUT / "v218-noise-coherence.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v218-noise-coherence.json")


if __name__ == "__main__":
    main()
