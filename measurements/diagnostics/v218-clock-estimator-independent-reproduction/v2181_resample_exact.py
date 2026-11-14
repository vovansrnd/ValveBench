#!/usr/bin/env python3
"""v2.18.1 п.5 — квантование resample_capture_timebase: точный шаг,
переходы округления, сравнение с дробной коррекцией.

Формула ревизии: Δppm = 1e6 / N_padded (шаг дискретности поправки).
Проверяется:
  * остаточный ppm по сетке ppm (лестница округления out_n);
  * переходы округления (где round() меняет out_n);
  * численный замер фактического масштаба времени для (a) округлённого
    resample_capture_timebase, (b) точной дробной коррекции (фазовый
    набег в частотной области — масштаб времени без квантования длины),
    (c) без коррекции;
  * влияние остаточного ε на фазу 10/20 кГц (потребители: sweep и
    noise_response — единственные применяющие коррекцию, см. аудит
    measure.py:888/746).
Playback НЕТ; production только импортируется.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench.measure import resample_capture_timebase  # noqa: E402
from v218_estimators_real import est_lsq  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
PAD_S = 0.25


def fractional_timebase(rec, ppm, fs):
    """Точная дробная коррекция: масштаб времени (1+ppm) без квантования
    длины — фазовый набег в частотной области + вырезка исходного интервала.
    Длина выхода НЕ округляется до целого числа отсчётов масштаба: выход
    сохраняет N отсчётов, но время масштабируется точно (фазовая наклейка)."""
    n = rec.shape[0]
    ratio = 1.0 + ppm * 1e-6
    # 1) ресэмплинг на сетку (1+ppm): n_out = N·ratio — ДРОБНЫЙ: берём
    #    ceil и обрезаем фазовым набегом последнего отсчёта не требуется —
    #    точность достигается интерполяцией по времени:
    #    y[k] = x(t_k / ratio), t_k = k/fs — линейная интерполяция исходных
    #    отсчётов (для band-limited 192к сигнала с контентом ≤ 24 кГц
    #    линейная интерполяция при масштабе 1.0000x вносит пренебрежимое
    #    искажение; документировано как ограничение метода)
    t_out = np.arange(n) / fs / ratio            # моменты источника
    out = np.empty_like(rec)
    for c in range(rec.shape[1]):
        out[:, c] = np.interp(t_out, np.arange(n) / fs, rec[:, c])
    return out


def actual_ppm_of_correction(n_in, n_out_corrected, ppm_intended):
    """Фактический масштаб: сколько времени занимает скорректированная
    запись при исходном fs относительно целевого масштаба."""
    expected = n_in * (1 + ppm_intended * 1e-6)
    return (n_out_corrected / expected - 1.0) * 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}

    # 1) точный шаг и переходы округления
    grid = []
    for dur_s in (5.0, 6.0, 7.0):
        n = int(round(dur_s * FS))
        padded_n = n + 2 * int(round(PAD_S * FS))
        dppm = 1e6 / padded_n
        res, trans = [], []
        prev = None
        for ppm in np.arange(-100.0, 100.001, 1.0):
            ratio = 1.0 + ppm * 1e-6
            out_n = max(1, int(round(padded_n * ratio)))
            r = (out_n / padded_n / ratio - 1.0) * 1e6
            res.append(round(r, 4))
            if prev is not None and out_n != prev:
                trans.append(round(ppm, 1))
            prev = out_n
        rows = {"capture_seconds": dur_s, "padded_frames": padded_n,
                "delta_ppm_exact": round(dppm, 4),
                "residual_min_ppm": min(res), "residual_max_ppm": max(res),
                "rounding_transitions_ppm_per_1ppm_grid": trans[:6]}
        grid.append(rows)
        print(f"  {dur_s:.1f} с (padded {padded_n}): Δppm точно = {dppm:.3f} | "
              f"остаток [{min(res):+.3f}, {max(res):+.3f}] | "
              f"переходов на сетке 1 ppm: {len(trans)}", flush=True)
    results["quantization"] = grid

    # 2) численный замер: округлённый vs дробный vs без коррекции
    # сигнал: пилот 1 кГц + тон 10 кГц и 20 кГц, отрендеренный на масштабе
    # (1+ppm_true); проверяется фаза после коррекции
    numeric = []
    for ppm_true in (-23.5, -1.0, 0.7, 1.0):
        n = int(6.0 * FS)
        t = np.arange(n) / FS
        ratio = 1.0 + ppm_true * 1e-6
        # рендер: D10-масштаб — частоты сдвинуты на (1+ppm_true)
        sig = np.zeros((n, 2))
        for f in (1000.0, 10000.0, 20000.0):
            amp = 0.3
            sig[:, 0] += amp * np.sin(2 * np.pi * f * ratio * t)
            sig[:, 1] = sig[:, 0]
        a = resample_capture_timebase(sig.copy(), ppm_true, FS)
        b = fractional_timebase(sig.copy(), ppm_true, FS)
        # фактическая частота после коррекции: est_lsq (независимый старт,
        # полосовая устойчивость к многотональному составу; 48 кГц —
        # однократная децимация, 20 кГц < Найквист 24 кГц)
        def measured_f(x, f_nom):
            x48 = resample_poly(x, 1, 4)
            return est_lsq(x48, 48000.0, f_nom, 48000)
        row = {"ppm_true": ppm_true}
        for f_nom in (1000.0, 10000.0, 20000.0):
            fa = measured_f(a[:, 0], f_nom)
            fb = measured_f(b[:, 0], f_nom)
            fnc = measured_f(sig[:, 0], f_nom)
            # остаток = отклонение частоты ПОСЛЕ коррекции от номинала
            # (двойной вычет ppm_true из v2.18.1-черновика исправлен:
            # для скорректированных сигналов остаток = (f/f_ном − 1)·1e6)
            row[f"residual_ppm_rounded_{int(f_nom/1000)}k"] = \
                round((fa / f_nom - 1) * 1e6, 3)
            row[f"residual_ppm_fractional_{int(f_nom/1000)}k"] = \
                round((fb / f_nom - 1) * 1e6, 3)
            row[f"residual_ppm_none_{int(f_nom/1000)}k"] = \
                round((fnc / f_nom - 1) * 1e6, 3)
        numeric.append(row)
        print(f"  ppm {ppm_true:+7.1f}: остаток округл. "
              f"{row['residual_ppm_rounded_20k']:+.2f} | дробн. "
              f"{row['residual_ppm_fractional_20k']:+.2f} | без корр. "
              f"{row['residual_ppm_none_20k']:+.1f} ppm (на 20 кГц)", flush=True)
    results["numeric"] = numeric

    # 3) влияние ε на фазу 10/20 кГц (потребители коррекции)
    impact = []
    for eps in (0.0, 0.3, 0.7, 1.0, 2.0):
        row = {"residual_ppm": eps}
        for f in (10000.0, 20000.0):
            for T in (2.0, 8.0):
                dphi = 2 * math.pi * f * T * eps * 1e-6
                row[f"phase_deg_{int(f/1000)}k_{int(T)}s"] = \
                    round(math.degrees(dphi), 2)
        impact.append(row)
    results["phase_impact"] = impact
    for row in impact:
        print(f"  ε={row['residual_ppm']} ppm: фаза 20к/2с "
              f"{row['phase_deg_20k_2s']}° | 10к/8с {row['phase_deg_10k_8s']}°",
              flush=True)

    json.dump(results, open(OUT / "v2181-resample-exact.json", "w",
              encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2181-resample-exact.json")


if __name__ == "__main__":
    main()
