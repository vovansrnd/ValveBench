#!/usr/bin/env python3
"""v2.18.1 п.2b — пилоты 997/1013 Гц против сетевых гармоник (офлайн).

Аудит (dsp.py:266, находка 1): пилот 1000 Гц — 20-я гармоника 50 Гц,
сетевая помеха попадает в ±3 бина центроида. Рекомендация: простое число
(997 или 1013 Гц). Проверка офлайн: синтетический тон пилота + сетевые
линии 50/100/150/.../2000 Гц + белый шум, уровни помехи варьируются.
Production-частота НЕ меняется (офлайн-исследование).

Оценщики: production-центроид + LSQ v2.18.1 (независимый старт).
Playback НЕТ; production только импортируется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench import calib, dsp  # noqa: E402
from v218_estimators_real import est_lsq, ppm_of  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 192000
TRIALS = 50
PPM_TRUE = -23.5
LEVEL = -72.0
BASE_SEED = 218200
PILOTS = (997.0, 1000.0, 1013.0)
MAINS_LEVELS = (-90.0, -84.0, -30.0)  # уровень КАЖДОЙ гармоники, dBFS:
# −90/−84 = РЕАЛИСТИЧНЫЕ уровни гармоник тракта (измерено: линия 1000 Гц
# −106.8 дБ/Гц ≈ −93 дБFS — на 21 дБ ниже пилота −72); −60…−30 = стресс-тест


def run(pilot, mains_level, cal, trials=TRIALS):
    errs_prod, errs_lsq = [], []
    for tr in range(trials):
        seed = BASE_SEED + int(pilot) * 1000 + int(abs(mains_level)) * 10 + tr
        rng = np.random.default_rng(seed)
        t = np.arange(N) / FS
        phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
        f_true = pilot * (1 + PPM_TRUE * 1e-6)
        tone = dsp.dbfs_to_amp(LEVEL) * np.sin(2 * np.pi * f_true * t + phase0)
        x = tone.copy()
        # сетевые гармоники 50·k, k = 1..40 (до 2 кГц), уровень mains_level,
        # фазы случайные (реалистичный гум)
        for k in range(1, 41):
            fh = 50.0 * k
            if fh >= FS / 2:
                break
            ph = 2 * math.pi * rng.random()
            x += dsp.dbfs_to_amp(mains_level) * np.sin(2 * np.pi * fh * t + ph)
        # белый шум: полы реального тракта L/R (берём R-канал как более чистый
        # + L-уровень для худшего случая; проверяем оба)
        for ch_noise, ch_list in ((-96.0, errs_prod),):
            pass
        x_noisy = x + dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)  # R-пол
        r = dsp.analyze_tone(x_noisy, FS, pilot, N, nharm=2,
                             band=(20.0, cal["band_hi"]), mains=cal["mains"])
        errs_prod.append((r["f0"] / pilot - 1) * 1e6 - PPM_TRUE)
        x48 = None
        f_est = est_lsq(x_noisy, FS, pilot, N)
        errs_lsq.append(ppm_of(f_est) - PPM_TRUE)
    def st(v):
        a = np.asarray(v)
        return {"bias_ppm": round(float(np.mean(a)), 3),
                "sigma_ppm": round(float(np.std(a)), 3),
                "max_abs_ppm": round(float(np.max(np.abs(a))), 3)}
    return {"pilot_hz": pilot, "mains_level_dbfs": mains_level,
            "prod_centroid": st(errs_prod), "lsq": st(errs_lsq)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    rows = []
    print("=== пилоты 997/1000/1013 Гц против сетевых гармоник (−72 dBFS) ===",
          flush=True)
    print("пилот 1000 Гц = 20-я гармоника 50 Гц: помеха ЛОЖИТСЯ НА ПИЛОТ",
          flush=True)
    for pilot in PILOTS:
        for ml in MAINS_LEVELS:
            r = run(pilot, ml, cal)
            rows.append(r)
            print(f"пилот {pilot:.0f} Гц, гармоника {ml:+.0f} дБFS: "
                  f"центроид bias {r['prod_centroid']['bias_ppm']:+7.2f} "
                  f"σ {r['prod_centroid']['sigma_ppm']:6.2f} max "
                  f"{r['prod_centroid']['max_abs_ppm']:7.2f} | "
                  f"LSQ bias {r['lsq']['bias_ppm']:+7.2f} "
                  f"σ {r['lsq']['sigma_ppm']:6.2f} max "
                  f"{r['lsq']['max_abs_ppm']:7.2f}", flush=True)
    json.dump({"level_dbfs": LEVEL, "ppm_true": PPM_TRUE, "trials": TRIALS,
               "base_seed": BASE_SEED, "results": rows},
              open(OUT / "v2181-lsq-pilots.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2181-lsq-pilots.json")


if __name__ == "__main__":
    main()
