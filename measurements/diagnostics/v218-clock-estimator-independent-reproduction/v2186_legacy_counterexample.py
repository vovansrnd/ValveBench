#!/usr/bin/env python3
"""v2.18.6 — воспроизведение семейства контрпримеров «сильная ближняя
линия» легаси-оценщиком (журнал §20.7 п.3) + арифметика исторических
−127 ppm.

Результаты (легаси 8d8b040, БЕЗ guard/QC, ВСЕ ошибки ПРИНИМАЮТСЯ):
  * линия 1000 Гц (−30 дБFS, пилот 997 −72 дБFS): захват МЕЖДУ пилотом и
    помехой, +1775.7 ppm;
  * линия НИЖЕ пилота (−4/−5 Гц, −30 дБFS): захват ВЕРХНЕГО края сетки
    фита, +153…156 ppm;
  * исторические −127.0 ppm (v2.18.4) — ТО ЖЕ семейство: арифметика
    точно соответствует НИЖНЕМУ краю сетки фита:
    (996.85/997 − 1)·1e6 − (−23.5) = −150.45 + 23.5 = −127.0 ppm.
    Направление захвата задаётся фазовым соотношением сигнал/помеха
    (кросс-члены RSS-ландшафта); v2.18.6-конструкция тянет вверх,
    историческая — вниз. Корневая причина одна: при помехе на десятки
    дБ громче пилота RSS-ландшафт окна фита определяется градиентом
    поглощения помехи, провал пилота (−42 дБ) в нём не виден.

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
from v2185_868_reproduction import est_lsq_8d8b040  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 48000.0
N = int(2.0 * FS)
BASE_SEED = 218660


def amp(dbfs):
    return 10 ** (dbfs / 20.0)


def run_variant(label, f_line, level, n_trials):
    f_pilot = 997.0 * (1 - 23.5e-6)
    t = np.arange(N) / FS
    errs = []
    for i in range(n_trials):
        rng = np.random.default_rng(BASE_SEED + abs(int(f_line * 10)) + i)
        x = amp(-72.0) * np.sin(2 * math.pi * f_pilot * t +
                                2 * math.pi * rng.random())
        x = x + amp(level) * np.sin(2 * math.pi * f_line * t +
                                    2 * math.pi * rng.random())
        x = x + amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)
        f_est, log = est_lsq_8d8b040(x, FS, 997.0)
        errs.append(round((f_est / 997.0 - 1) * 1e6 + 23.5, 1))
    return {"variant": label, "f_line_hz": round(f_line, 2),
            "level_dbfs": level, "n": n_trials,
            "err_ppm_median": float(np.median(errs)),
            "err_ppm_mean": round(float(np.mean(errs)), 1),
            "err_ppm_min": min(errs), "err_ppm_max": max(errs)}


def main():
    variants = [
        run_variant("линия 1000 Гц (контрпример v2.18.4)",
                    1000.0 * (1 - 23.5e-6), -30.0, 100),
        run_variant("линия 993 Гц (−4 Гц от пилота)", 993.0, -30.0, 50),
        run_variant("линия 992 Гц (−5 Гц от пилота)", 992.0, -30.0, 50),
    ]
    edge_arith = {
        "historical_err_ppm": -127.0,
        "explanation": "(996.85/997 − 1)·1e6 − (−23.5) = −150.45 + 23.5 = "
                       "−127.0 ppm — НИЖНИЙ край сетки фита ±0.15 Гц у "
                       "оценщика v2.18.4; направление захвата (верхний/"
                       "нижний край или середина) задаётся фазовым "
                       "соотношением сигнал/помеха через кросс-члены "
                       "RSS-ландшафта. Семейство одно: RSS-ландшафт окна "
                       "фита при помехе на десятки дБ громче пилота "
                       "определяется градиентом поглощения помехи.",
        "v2186_behavior": "все варианты семейства ОТКЛОНЯЮТСЯ с атрибуцией "
                          "(line_contaminated, line_hz у помехи) — "
                          "v2186-qc-confusion.json: TP_named 1000/1000",
    }
    print(json.dumps({"variants": variants, "edge_arithmetic": edge_arith},
                     ensure_ascii=False, indent=1), flush=True)
    json.dump({"variants": variants, "edge_arithmetic": edge_arith},
              open(OUT / "v2186-legacy-counterexample.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v2186-legacy-counterexample.json")


if __name__ == "__main__":
    main()
