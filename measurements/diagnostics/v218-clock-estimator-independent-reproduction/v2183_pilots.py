#!/usr/bin/env python3
"""v2.18.3 п.5 — пилоты 997/1000/1013 Гц с ИЗМЕРЕННЫМ профилем гармоник.

Ревизия v2.18.2 п.5: 40 гармоник ОДИНАКОВОГО уровня — искусственная
гребёнка, не реальный спектр. Здесь: уровни КАЖДОЙ гармоники измерены
из ансамбля PSD тракта (v2.18 модель, 129 окон) — реальный профиль.
Равномерная гребёнка сохранена отдельным стресс-тестом.

err = (f_est/pilot − 1)·1e6 − PPM_TRUE; сырые f_est per-trial;
SHA256 скрипта. Playback НЕТ; production только импортируется.
"""
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
from v2183_estimators import est_lsq, ppm_of_pilot  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 48000.0
N = int(2.0 * FS)
TRIALS = 100
LEVEL = -72.0
PPM_TRUE = -23.5
BASE_SEED = 218500
PILOTS = (997.0, 1000.0, 1013.0)
# измеренный профиль (v2.18 ансамбль PSD, дБ/Гц → амплитуда линии в
# эффективной полосе линии ~23 Гц: A = sqrt(PSD·23.4))
MEASURED_PROFILE_DBHZ = {50: -109.5, 100: -112.5, 150: -110.0, 200: -117.0,
                         250: -116.0, 300: -115.0, 350: -115.0, 400: -116.0,
                         450: -115.0, 500: -114.9, 550: -115.5, 600: -116.0,
                         650: -116.5, 700: -117.0, 750: -117.5, 800: -118.9,
                         850: -118.0, 900: -118.5, 950: -118.0,
                         1000: -106.8, 1050: -118.0, 1100: -118.5,
                         1150: -119.0, 1200: -119.1, 1250: -120.0,
                         1300: -123.2, 1350: -121.0, 1400: -120.5,
                         1450: -121.0, 1500: -119.1, 1550: -120.0,
                         1600: -121.0, 1650: -122.0, 1700: -121.5,
                         1750: -122.0, 1800: -122.5, 1850: -123.0,
                         1900: -123.5, 1950: -124.0, 2000: -119.1}
UNIFORM_COMB_LEVEL = -60.0            # стресс-тест: 40 линий по −60 дБFS


def harmonic_amp(fh, level_dbfs=None):
    """Амплитуда гармоники: измеренный профиль (дБ/Гц → дБFS линии)
    или фиксированный уровень (стресс-тест)."""
    if level_dbfs is not None:
        return 10 ** (level_dbfs / 20.0)
    psd_dbhz = MEASURED_PROFILE_DBHZ.get(int(fh))
    if psd_dbhz is None:
        return 0.0
    psd = 10 ** (psd_dbhz / 10.0)
    return math.sqrt(psd * 23.4)       # амплитуда линии в полосе ~23 Гц


def run(pilot, mode, mains_level, cal, trials=TRIALS):
    errs_prod, errs_lsq = [], []
    raw = []
    for tr in range(trials):
        seed = BASE_SEED + int(pilot) * 100 + (hash((mode, mains_level)) %
                                               1000) * 10 + tr
        rng = np.random.default_rng(seed)
        t = np.arange(N) / FS
        phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
        f_true = pilot * (1 + PPM_TRUE * 1e-6)
        x = dsp.dbfs_to_amp(LEVEL) * np.sin(2 * math.pi * f_true * t + phase0)
        for k in range(1, 41):
            fh = 50.0 * k
            if fh >= FS / 2:
                break
            if mode == "measured":
                amp = harmonic_amp(fh)
            else:
                amp = harmonic_amp(fh, mains_level)
            if amp > 0:
                x += amp * np.sin(2 * math.pi * fh * t +
                                  2 * math.pi * rng.random())
        x += dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)
        r = dsp.analyze_tone(x, FS, pilot, N, nharm=2,
                             band=(20.0, cal["band_hi"]), mains=cal["mains"])
        errs_prod.append((r["f0"] / pilot - 1) * 1e6 - PPM_TRUE)
        f_est, fit_rate = est_lsq(x, FS, pilot)
        errs_lsq.append((f_est / pilot - 1) * 1e6 - PPM_TRUE)
        raw.append({"seed": seed, "f_est_hz": round(f_est, 4),
                    "fit_rate_hz": fit_rate})
    def st(v):
        a = np.asarray(v)
        return {"bias_ppm": round(float(np.mean(a)), 3),
                "sigma_ppm": round(float(np.std(a)), 3),
                "max_abs_ppm": round(float(np.max(np.abs(a))), 3)}
    return {"pilot_hz": pilot, "mode": mode,
            "mains_level_dbfs": mains_level, "trials": trials,
            "prod_centroid": st(errs_prod), "lsq": st(errs_lsq),
            "lsq_fit_rate_min_hz": min(r["fit_rate_hz"] for r in raw),
            "lsq_fit_rate_max_hz": max(r["fit_rate_hz"] for r in raw),
            "per_trial": raw}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    script_hash = hashlib.sha256(open(Path(__file__).resolve(), "rb").read()).hexdigest()
    print(f"script sha256: {script_hash}", flush=True)
    rows = []
    print("=== РЕАЛИСТИЧНЫЙ профиль гармоник (измеренные уровни) ===",
          flush=True)
    for pilot in PILOTS:
        r = run(pilot, "measured", None, cal)
        rows.append(r)
        print(f"пилот {pilot:.0f}: центроид bias {r['prod_centroid']['bias_ppm']:+7.2f} "
              f"σ {r['prod_centroid']['sigma_ppm']:6.2f} | "
              f"LSQ bias {r['lsq']['bias_ppm']:+7.2f} σ {r['lsq']['sigma_ppm']:6.2f} | "
              f"фит {r['lsq_fit_rate_min_hz']:.0f} Гц", flush=True)
    print("=== СТРЕСС-ТЕСТ: равномерная гребёнка −60 дБFS × 40 ===", flush=True)
    for pilot in PILOTS:
        r = run(pilot, "uniform_comb", UNIFORM_COMB_LEVEL, cal, trials=50)
        rows.append(r)
        print(f"пилот {pilot:.0f}: центроид bias {r['prod_centroid']['bias_ppm']:+7.2f} "
              f"σ {r['prod_centroid']['sigma_ppm']:6.2f} | "
              f"LSQ bias {r['lsq']['bias_ppm']:+7.2f} σ {r['lsq']['sigma_ppm']:6.2f}",
              flush=True)
    json.dump({"script_sha256": script_hash, "level_dbfs": LEVEL,
               "ppm_true": PPM_TRUE, "trials": TRIALS,
               "measured_profile_dbhz": MEASURED_PROFILE_DBHZ,
               "results": rows},
              open(OUT / "v2183-lsq-pilots.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2183-lsq-pilots.json")


if __name__ == "__main__":
    main()
