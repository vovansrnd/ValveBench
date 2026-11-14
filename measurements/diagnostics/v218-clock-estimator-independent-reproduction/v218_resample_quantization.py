#!/usr/bin/env python3
"""v2.18 п.5 — количественная проверка квантования resample_capture_timebase.

out_n = round(padded_n·ratio) и target_n = round(data_n·actual_ratio) дают
остаточную ошибку масштаба времени. Проверяется:
  * фактический остаточный ppm для длин захвата 5–7 с при 192 кГц по сетке
    ppm (какого масштаба дискретность поправки);
  * влияние остаточного ppm на H1: набег фазы 2π·f·T·ε и потеря когерентности
    |γ|² для f ∈ {1, 10, 20} кГц, T ∈ {1, 2, 8} с;
  * численная проверка на синтетике: после resample_capture_timebase()
    измерить фактический масштаб времени по вставленному пилоту.
Playback НЕТ; production только импортируется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from valvebench.measure import resample_capture_timebase  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
PAD_S = 0.25


def quantization_grid():
    rows = []
    for dur_s in (5.0, 5.5, 6.0, 6.5, 7.0):
        n = int(round(dur_s * FS))
        padded_n = n + 2 * int(round(PAD_S * FS))
        res = []
        for ppm in np.arange(-100.0, 100.01, 2.5):
            ratio = 1.0 + ppm * 1e-6
            out_n = max(1, int(round(padded_n * ratio)))
            actual = out_n / padded_n
            res_ppm = (actual / ratio - 1.0) * 1e6
            res.append(res_ppm)
        res = np.asarray(res)
        rows.append({"capture_seconds": dur_s, "padded_frames": padded_n,
                     "residual_ppm_min": round(float(res.min()), 3),
                     "residual_ppm_max": round(float(res.max()), 3),
                     "residual_ppm_step": round(float(res.max() - res.min()), 3),
                     "max_abs_residual_ppm": round(float(np.max(np.abs(res))), 3)})
        print(f"  {dur_s:.1f} с (padded {padded_n}): остаточный ppm "
              f"[{res.min():+.3f}, {res.max():+.3f}], шаг {res.max()-res.min():.3f}, "
              f"макс |ε| {np.max(np.abs(res)):.3f}", flush=True)
    return rows


def numeric_check():
    """Численная проверка фактического масштаба времени после коррекции."""
    out = []
    for ppm in (-23.5, -5.0, 0.7, 1.0):
        n = int(6.0 * FS)
        t = np.arange(n) / FS
        # сигнал: пилот 1 кГц, отрендеренный с масштабом (1+ppm) — как D10
        f = 1000.0 * (1 + ppm * 1e-6)
        rec = np.column_stack([np.sin(2 * np.pi * f * t)] * 2)
        corrected = resample_capture_timebase(rec, ppm, FS)
        # фактический масштаб: длина corrected против ожидаемой
        expected_n = n * (1 + ppm * 1e-6)
        out.append({"ppm_in": ppm,
                    "length_in": n, "length_corrected": len(corrected),
                    "expected_length": round(expected_n, 2),
                    "residual_ppm_from_length":
                        round((len(corrected) / expected_n - 1) * 1e6, 3)})
        print(f"  ppm {ppm:+7.1f}: длина {n} → {len(corrected)} "
              f"(ожидается {expected_n:.1f}), остаток "
              f"{out[-1]['residual_ppm_from_length']:+.3f} ppm", flush=True)
    return out


def h1_impact():
    rows = []
    for eps in (0.05, 0.1, 0.3, 0.7, 1.0, 2.0):
        row = {"residual_ppm": eps}
        for f in (1000.0, 10000.0, 20000.0):
            for T in (1.0, 2.0, 8.0):
                dphi = 2 * math.pi * f * T * eps * 1e-6
                # когерентный gain линейного набега фазы по окну
                g = abs(math.sin(dphi / 2) / (dphi / 2)) if dphi > 0 else 1.0
                row[f"phase_deg_{int(f/1000)}k_{int(T)}s"] = round(math.degrees(dphi), 3)
                row[f"gamma_loss_pct_{int(f/1000)}k_{int(T)}s"] = round((1 - g * g) * 100, 4)
        rows.append(row)
    return rows


def main():
    print("=== сетка квантования out_n/actual_ratio ===", flush=True)
    grid = quantization_grid()
    print("=== численная проверка фактического масштаба ===", flush=True)
    numeric = numeric_check()
    print("=== влияние остаточного ppm на H1 (фаза/когерентность) ===", flush=True)
    impact = h1_impact()
    json.dump({"quantization_grid": grid, "numeric_check": numeric,
               "h1_impact": impact},
              open(OUT / "v218-resample-quantization.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v218-resample-quantization.json")


if __name__ == "__main__":
    main()
