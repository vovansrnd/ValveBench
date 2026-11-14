#!/usr/bin/env python3
"""v2.18.2 п.3 — пилоты 997/1000/1013 Гц: ИСПРАВЛЕННАЯ метрика.

Исправление ревизии v2.18.1: ppm_of() в v218_estimators_real.py жёстко
делит на 1000 Гц — для пилотов 997/1013 «ошибка» содержала разницу
номиналов (−3000/+13000 ppm) даже при правильной оценке. Здесь:
  err_ppm = (f_est/pilot − 1)·1e6 − PPM_TRUE   — относительно ПИЛОТА;
  сырые f_est сохраняются per-trial;
  SHA256 скрипта сохраняется (воспроизводимость версии);
  невозможный результат ~868 Гц (v2.18.1) расследуется: committed
  est_lsq ищет в ±2 Гц грубо и ±0.15 Гц тонко — выход за пределы
  документируется отдельно.

Playback НЕТ; production только импортируется.
"""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench import calib, dsp  # noqa: E402
from v218_estimators_real import est_lsq  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 48000.0                    # MC-частота (однократная децимация)
N = int(2.0 * FS)
TRIALS = 100
LEVEL = -72.0
PPM_TRUE = -23.5
BASE_SEED = 218400
PILOTS = (997.0, 1000.0, 1013.0)
# уровни сетевых гармоник: реалистичный (измеренный: линия 1000 Гц
# −106.8 дБ/Гц ≈ −93 дБFS) + стресс-уровни
MAINS_LEVELS = (-93.0, -80.0, -60.0, -30.0)


def err_ppm(f_est, pilot):
    return (f_est / pilot - 1.0) * 1e6 - PPM_TRUE


def run(pilot, mains_level, cal, trials=TRIALS):
    raw = []
    for tr in range(trials):
        seed = BASE_SEED + int(pilot) * 100 + int(abs(mains_level)) + tr
        rng = np.random.default_rng(seed)
        t = np.arange(N) / FS
        phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
        f_true = pilot * (1 + PPM_TRUE * 1e-6)
        x = dsp.dbfs_to_amp(LEVEL) * np.sin(2 * np.pi * f_true * t + phase0)
        # сетевые гармоники 50·k, k = 1..40 (до 2 кГц), случайные фазы
        for k in range(1, 41):
            fh = 50.0 * k
            if fh >= FS / 2:
                break
            x += dsp.dbfs_to_amp(mains_level) * np.sin(
                2 * math.pi * fh * t + 2 * math.pi * rng.random())
        x += dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)   # пол R
        f_est = est_lsq(x, FS, pilot, N)
        raw.append({"seed": seed, "f_est_hz": round(f_est, 4),
                    "err_ppm": round(err_ppm(f_est, pilot), 3)})
    a = np.asarray([r["err_ppm"] for r in raw])
    return {"pilot_hz": pilot, "mains_level_dbfs": mains_level,
            "trials": trials,
            "f_est_min_hz": round(min(r["f_est_hz"] for r in raw), 3),
            "f_est_max_hz": round(max(r["f_est_hz"] for r in raw), 3),
            "bias_ppm": round(float(np.mean(a)), 3),
            "sigma_ppm": round(float(np.std(a)), 3),
            "max_abs_ppm": round(float(np.max(np.abs(a))), 3),
            "per_trial": raw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    script_hash = hashlib.sha256(open(Path(__file__).resolve(), "rb").read()).hexdigest()
    print(f"script sha256: {script_hash}", flush=True)
    rows = []
    for pilot in PILOTS:
        for ml in MAINS_LEVELS:
            r = run(pilot, ml, cal)
            rows.append(r)
            print(f"пилот {pilot:.0f} Гц, гармоника {ml:+.0f} дБFS: "
                  f"bias {r['bias_ppm']:+8.2f} σ {r['sigma_ppm']:6.2f} "
                  f"max {r['max_abs_ppm']:8.2f} | f_est "
                  f"[{r['f_est_min_hz']:.2f}, {r['f_est_max_hz']:.2f}] Гц",
                  flush=True)
    # расследование невозможного 868 Гц из v2.18.1
    inv = {
        "claim": ("v2.18.1 JSON содержал LSQ-оценку, соответствующую "
                  "~868 Гц для пилота 997/−30 dBFS"),
        "committed_est_lsq_search_bounds": {
            "coarse_hz": 2.0, "refine_hz": 0.15,
            "max_deviation_from_pilot_hz": 2.15,
            "max_deviation_ppm": round(2.15 / 997 * 1e6, 1)},
        "rerun_result": {},
    }
    r = run(997.0, -30.0, cal, trials=50)
    inv["rerun_result"] = {"pilot_hz": 997.0, "mains_level_dbfs": -30.0,
                           "trials": 50,
                           "f_est_min_hz": r["f_est_min_hz"],
                           "f_est_max_hz": r["f_est_max_hz"],
                           "bias_ppm": r["bias_ppm"],
                           "sigma_ppm": r["sigma_ppm"],
                           "conclusion": ("committed est_lsq не может вернуть "
                                          "868 Гц: поиск ограничен "
                                          "997±2.15 Гц; результат v2.18.1 "
                                          "был получен более ранней версией "
                                          "кода (JSON stale)")}
    print(json.dumps(inv["rerun_result"], ensure_ascii=False), flush=True)
    json.dump({"script_sha256": script_hash, "level_dbfs": LEVEL,
               "ppm_true": PPM_TRUE, "trials": TRIALS, "results": rows,
               "investigation_868hz": inv},
              open(OUT / "v2182-lsq-pilots.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2182-lsq-pilots.json")


if __name__ == "__main__":
    main()
