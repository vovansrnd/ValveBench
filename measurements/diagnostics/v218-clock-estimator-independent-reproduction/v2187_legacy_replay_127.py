#!/usr/bin/env python3
"""v2.18.7 — ТОЧНОЕ воспроизведение исторических −127 ppm (§21 п.4).

Оценщик: ТОЧНАЯ копия est_lsq из v2184_estimators.py (ветка
agent/v2184-clock-estimator-evidence-closure, HEAD 4fc938e) —
bounded-coarse с багом curv > 0 (уточнение только на минимумах),
guard edge_peak, RSS-сетка ±0.15 Гц. НЕ подменяется 8d8b040
(другой исторический алгоритм — его семейство +1775.7 ppm
сохранено отдельно в v2186-legacy-counterexample.json).

Конструкция (реконструкция, проверена воспроизведением −127.0):
  Fs 192 кГц, длительность 2 с (N = 384000), ШТАТНАЯ децимация
  resample_poly(1, 4) → 48 кГц ВНУТРИ est_lsq;
  пилот 997·(1−23.5e-6) Гц, −60 дБFS, фаза phase0-формула v2.18.1
  (2π·((seed·37) % 997)/997);
  линия 1000·(1−23.5e-6) Гц, −30 дБFS, фаза 2π·rng.random();
  шум −96 дБFS rng.standard_normal;
  seed = 218200 + 997000 + 300 + tr (формула v2181_lsq_pilots.py).

Уровень пилота −60 дБFS reconstructed: он необходим, чтобы coarse-пик
остался НА ПИЛОТЕ (утечка линии −30 дБFS в бин 998.5 ≈ −31.5 дБ
первый боковик Ханна ~ 20 ед. |FFT| против 6 у пилота −72 — при −72
coarse уходит на край полосы, что даёт ДРУГУЮ семью edge_peak).
Длительность 2 с: варианты 1 с @ 192 кГц дают edge_peak 100/100, НЕ
−127; 2 с — дефолтная шкала скриптов v2.18.4 (FS=48000, N=96000).

Механизм (логируется per-trial): coarse-пик на 997.0 (баг curv > 0
пропускает валидное уточнение вогнутого пика) → RSS-сетка
[996.85, 997.15] → аргмин на КРАЕ (нижний −127.0 или верхний +174.0 —
направление задаётся фазой линии через кросс-члены ландшафта),
статус ok ВСЕГДА — оценка с ошибкой ±127…174 ppm ПРИНИМАЕТСЯ молча.
v2.18.7: те же сигналы → line_contaminated с атрибуцией линии.

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
from v2187_estimators import QC_CFG, est_lsq, residual_line_qc  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 384000                      # 2 с @ 192 кГц
PILOT = 997.0
PPM_TRUE = -23.5
PILOT_LEVEL = -60.0
LINE_LEVEL = -30.0
BASE_SEED = 218200              # формула v2181_lsq_pilots.py


def amp(db):
    return 10 ** (db / 20.0)


def est_lsq_v2184_logged(seg, fs, f_nom, coarse_hz=2.0):
    """ТОЧНАЯ копия est_lsq из v2184_estimators.py (4fc938e) с
    диагностическим логом; арифметика не изменена."""
    log = {}
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    log["fs_after_decimation"] = fs
    n = len(seg)
    t = np.arange(n) / fs
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.arange(len(X)) * (fs / n)
    band = (freqs >= f_nom - coarse_hz) & (freqs <= f_nom + coarse_hz)
    ib = np.where(band)[0]
    k = int(ib[int(np.argmax(X[ib]))])
    log["coarse_peak_hz"] = round(k * fs / n, 3)
    log["coarse_at_band_edge"] = bool(k == ib[0] or k == ib[-1])
    status = "ok"
    if k == ib[0] or k == ib[-1]:
        status = "edge_peak"                     # guard v2.18.4
    if 0 < k < len(X) - 1:
        y0, y1, y2 = X[k - 1], X[k], X[k + 1]
        curv = y0 + y2 - 2 * y1
        log["coarse_neighbors"] = [round(float(y0), 3), round(float(y1), 3),
                                   round(float(y2), 3)]
        log["coarse_curv"] = round(float(curv), 4)
        log["curv_check_v2184"] = ("refine applied (BUG: curv > 0 — только"
                                   " на минимумах)" if curv > 0 else
                                   "refine SKIPPED (вогнутый пик, curv < 0)")
        if curv > 0:
            k_f = k + np.clip((y0 - y2) / (2 * curv), -0.5, 0.5)
        else:
            k_f = float(k)
    else:
        k_f = float(k)
    f0 = k_f * fs / n
    log["f0_after_coarse_hz"] = round(f0, 4)
    grid = np.linspace(f0 - 0.15, f0 + 0.15, 25)
    log["rss_grid_hz"] = [round(grid[0], 4), round(grid[-1], 4)]
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * math.pi * f * t),
                             np.cos(2 * math.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    log["rss_argmin_index"] = i
    log["rss_argmin_at_endpoint"] = bool(i == 0 or i == len(grid) - 1)
    f_best = grid[i]
    if 0 < i < len(grid) - 1:
        curv2 = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if curv2 > 0:
            f_best += np.clip((rss[i - 1] - rss[i + 1]) / (2 * curv2) *
                              (grid[1] - grid[0]),
                              -(grid[1] - grid[0]), grid[1] - grid[0])
    log["f_est_hz"] = round(f_best, 4)
    log["status"] = status
    return f_best, status, log


def make_trial(tr):
    seed = BASE_SEED + int(PILOT) * 1000 + int(abs(LINE_LEVEL)) * 10 + tr
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
    f_pilot = PILOT * (1 + PPM_TRUE * 1e-6)
    f_line = 1000.0 * (1 + PPM_TRUE * 1e-6)
    x = amp(PILOT_LEVEL) * np.sin(2 * math.pi * f_pilot * t + phase0)
    x += amp(LINE_LEVEL) * np.sin(2 * math.pi * f_line * t +
                                  2 * math.pi * rng.random())
    x += amp(-96.0) * rng.standard_normal(N)
    return x, f_line, seed


def main():
    trials = []
    for tr in range(100):
        x, f_line, seed = make_trial(tr)
        f_est, status, log = est_lsq_v2184_logged(x, FS, PILOT)
        err = (f_est / PILOT - 1) * 1e6 - PPM_TRUE
        # атрибуция линии современным QC (по остатку исторического фита)
        seg48 = resample_poly(x, 1, 4)
        n48 = len(seg48)
        t48 = np.arange(n48) / 48000.0
        D = np.column_stack([np.sin(2 * math.pi * f_est * t48),
                             np.cos(2 * math.pi * f_est * t48),
                             np.ones(n48)])
        coef, *_ = np.linalg.lstsq(D, seg48, rcond=None)
        resid = seg48 - D @ coef
        Wr = np.abs(np.fft.rfft(resid * np.hanning(n48)))
        fr = np.arange(len(Wr)) * (48000.0 / n48)
        carrier_ref = math.hypot(coef[0], coef[1]) * \
            float(np.hanning(n48).sum()) / 2.0
        qc = residual_line_qc(Wr, fr, carrier_ref, f_est, 48000.0, QC_CFG)
        log["err_ppm"] = round(err, 2)
        log["line_attribution_modern_qc"] = {
            "line": qc["line"], "line_hz": qc["line_hz"],
            "line_dbc": qc["line_dbc"]}
        log["trial"] = tr
        log["seed"] = seed
        trials.append(log)
        if tr < 3 or (tr % 25 == 0):
            print(f"  tr={tr}: статус {status}, f_est {f_est:.4f} Гц, "
                  f"err {err:+.2f} ppm, coarse {log['coarse_peak_hz']}, "
                  f"аргмин {'КРАЙ' if log['rss_argmin_at_endpoint'] else i}",
                  flush=True)
    n_low = sum(1 for t_ in trials if abs(t_["err_ppm"] + 127.0) < 0.3)
    n_high = sum(1 for t_ in trials if abs(t_["err_ppm"] - 174.0) < 0.3)
    n_ok_status = sum(1 for t_ in trials if t_["status"] == "ok")
    summary = {
        "estimator": "точная копия est_lsq из v2184_estimators.py "
                     "(4fc938e) — bounded-coarse, баг curv > 0, guard, "
                     "RSS ±0.15 Гц; НЕ 8d8b040",
        "construction": {
            "fs_hz": FS, "duration_s": N / FS,
            "decimation": "resample_poly(1, 4) → 48 кГц внутри est_lsq",
            "pilot_hz": PILOT, "pilot_level_dbfs": PILOT_LEVEL,
            "line_hz": 1000.0 * (1 + PPM_TRUE * 1e-6),
            "line_level_dbfs": LINE_LEVEL,
            "noise_dbfs": -96.0,
            "seed_formula": "218200 + 997000 + 300 + tr (tr 0..99)",
            "reconstruction_notes": [
                "уровень пилота −60 дБFS: при −72 coarse уходит на край "
                "полосы (утечка линии в бин 998.5, первый боковик Ханна "
                "−31.5 дБ) — семья edge_peak, не −127",
                "длительность 2 с: вариант 1 с @ 192 кГц даёт edge_peak "
                "100/100 (гл. лепесток Ханна ±2 Гц при T=1 с накрывает "
                "бин 999); 2 с — дефолт скриптов v2.18.4",
            ],
        },
        "trials": 100,
        "status_ok_count": n_ok_status,
        "err_minus_127_ppm": n_low,
        "err_plus_174_ppm": n_high,
        "verdict": None,
        "example_trial": trials[0],
        "v2187_behavior": "те же сигналы через v2187_estimators.est_lsq: "
                          "line_contaminated с атрибуцией (см. per-trial "
                          "line_attribution_modern_qc)",
    }
    # v2.18.7 на тех же сигналах
    refused = 0
    for tr in range(100):
        x, f_line, seed = make_trial(tr)
        qc = est_lsq(x, FS, PILOT)
        if qc["status"] == "line_contaminated":
            refused += 1
    summary["v2187_refused_of_100"] = refused
    if n_low + n_high == 100 and n_ok_status == 100:
        summary["verdict"] = (
            f"ТОЧНОЕ ВОСПРОИЗВЕДЕНИЕ: {n_low}/100 −127.0 ppm (нижний край "
            f"996.85), {n_high}/100 +174.0 ppm (верхний край 997.15), "
            f"статус ok 100/100 — историческая ошибка ПРИНИМАЛАСЬ молча; "
            f"v2.18.7 отклоняет {refused}/100")
    else:
        summary["verdict"] = (f"исходы: −127.0 {n_low}/100, +174.0 "
                              f"{n_high}/100, ok {n_ok_status}/100, "
                              f"v2.18.7 отказ {refused}/100")
    print(json.dumps(summary, ensure_ascii=False, indent=1)[:1200],
          flush=True)
    json.dump({"summary": summary, "per_trial": trials},
              open(OUT / "v2187-legacy-replay-127.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v2187-legacy-replay-127.json")


if __name__ == "__main__":
    main()
