#!/usr/bin/env python3
"""v2.18.5 — пилоты с профилем гармоник, ИЗВЛЕЧЁННЫМ ИЗ WAV.

Ревизия v2.18.3 п.5: 40 гармоник одинакового уровня — искусственная
гребёнка; уровни были статическим словарём, не из WAV; seed содержал
недетерминированный hash(). Здесь:
  * профиль гармоник извлекается из ансамбля PSD тихих окон ЗАХВАТОВ
    (интегрирование мощности каждой линии в её лепестке ±12 Гц);
  * корректный переход мощность → пиковая амплитуда: A = √(2·P)
    (мощность синусоиды = A²/2 → RMS→peak = ×√2);
  * seed детерминированный: BASE + pilot·100 + condition·10 + tr —
    БЕЗ недетерминированного hash() (находка ревизии v2.18.3);
  * равномерная гребёнка −60 дБFS — отдельный стресс-тест.

Playback НЕТ; production только импортируется.
"""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import csd, welch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench import calib, dsp  # noqa: E402
from v2185_estimators import est_lsq, rms_dbfs  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS_CAP = 192000
FS = 48000.0
N = int(2.0 * FS)
TRIALS = 100
LEVEL = -72.0
PPM_TRUE = -23.5
BASE_SEED = 218700
PILOTS = (997.0, 1000.0, 1013.0)
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
UNIFORM_COMB_LEVEL = -60.0


def quiet_windows(cap):
    w = int(0.05 * FS_CAP)
    m = len(cap) // w * w
    rms = np.sqrt(np.mean(cap[:m].reshape(-1, w, 2) ** 2, axis=1)).max(axis=1) * math.sqrt(2)
    thr = float(np.percentile(rms, 25)) * 10 ** (3.0 / 20.0)
    quiet = rms < thr
    out, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * 0.05 >= 0.35:
                for k in range(int((j - i) * 0.05 / 0.25)):
                    s = int((i * 0.05 + k * 0.25) * FS_CAP)
                    out.append((s, s + int(0.25 * FS_CAP)))
            i = j
        else:
            i += 1
    return out


def measured_harmonic_profile(source_dir):
    """Профиль гармоник ИЗ WAV: для каждой линии 50·k интегрируется
    мощность её лепестка (±12 Гц) по усреднённому ансамблю PSD тихих окон;
    пиковая амплитуда A = √(2·P) (мощность синусоиды = A²/2)."""
    nper = 8192
    S_LL = S_RR = S_LR = None
    n_win = 0
    for idx in IDX:
        _, cap = wavfile.read(str(Path(source_dir) / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        for (a, b) in quiet_windows(cap):
            f, P_LL = welch(cap[a:b, 0], FS_CAP, nperseg=nper)
            _, P_RR = welch(cap[a:b, 1], FS_CAP, nperseg=nper)
            _, P_LR = csd(cap[a:b, 0], cap[a:b, 1], FS_CAP, nperseg=nper)
            S_LL = P_LL if S_LL is None else S_LL + P_LL
            S_RR = P_RR if S_RR is None else S_RR + P_RR
            S_LR = P_LR if S_LR is None else S_LR + P_LR
            n_win += 1
    S_LL /= n_win; S_RR /= n_win; S_LR /= n_win
    df_w = float(f[1] - f[0])          # 23.4 Гц — шаг сетки Welch
    profile = {}
    for k in range(1, 41):
        fh = 50.0 * k
        if fh >= FS_CAP / 2 - 50:
            break
        m = (f >= fh - 12) & (f <= fh + 12)
        # ДИСКРЕТНОЕ интегрирование: Σ S·df (trapz над 1-2 бинами давал 0 —
        # на сетке 23.4 Гц лепесток линии содержит 1-2 бина)
        pL = float(np.sum(S_LL[m]) * df_w)
        pR = float(np.sum(S_RR[m]) * df_w)
        # КРОСС-СПЕКТРАЛЬНАЯ ФАЗА линии (ревизия v2.18.4 п.4): угол
        # среднего комплексного S_LR в лепестке = фаза R относительно L
        # на этой частоте; в синтезе R-линия получает ph_L + phase_LR
        s_lr_mean = complex(np.mean(S_LR[m]))
        phase_lr = round(math.atan2(s_lr_mean.imag, s_lr_mean.real), 4)
        # пиковая амплитуда синусоиды с мощностью P: A = √(2·P)
        profile[fh] = {"amp_L": round(math.sqrt(2 * pL), 8),
                       "amp_R": round(math.sqrt(2 * pR), 8),
                       "phase_LR_rad": phase_lr}
    return profile, n_win


def run(pilot, mode, profile, cal, trials=TRIALS, cond_no=0):
    # ОБА КАНАЛА (ревизия v2.18.4 п.4): линии синтезируются с измеренной
    # кросс-спектральной фазой phase_LR, оцениваются и сравниваются
    # per-channel (prod-центроид и LSQ на L и R раздельно)
    errs = {"prod_L": [], "prod_R": [], "lsq_L": [], "lsq_R": []}
    raw = []
    for tr in range(trials):
        seed = BASE_SEED + int(pilot) * 100 + cond_no * 10 + tr   # БЕЗ hash()
        rng = np.random.default_rng(seed)
        t = np.arange(N) / FS
        phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
        f_true = pilot * (1 + PPM_TRUE * 1e-6)
        x = np.zeros((N, 2))
        x[:, 0] = dsp.dbfs_to_amp(LEVEL) * np.sin(2 * math.pi * f_true * t +
                                                  phase0)
        x[:, 1] = dsp.dbfs_to_amp(LEVEL) * np.sin(2 * math.pi * f_true * t +
                                                  phase0)
        for k in range(1, 41):
            fh = 50.0 * k
            if fh >= FS / 2:
                break
            ph = 2 * math.pi * rng.random()
            if mode == "measured":
                a = profile.get(fh, {"amp_L": 0.0, "amp_R": 0.0,
                                     "phase_LR_rad": 0.0})
                # линии с ИЗМЕРЕННЫМИ амплитудами и КРОСС-ФАЗОЙ per-channel
                x[:, 0] += a["amp_L"] * np.sin(2 * math.pi * fh * t + ph)
                x[:, 1] += a["amp_R"] * np.sin(2 * math.pi * fh * t + ph +
                                               a["phase_LR_rad"])
            else:                              # стресс: гребёнка −60 дБFS
                a = dsp.dbfs_to_amp(UNIFORM_COMB_LEVEL)
                x[:, 0] += a * np.sin(2 * math.pi * fh * t + ph)
                x[:, 1] += a * np.sin(2 * math.pi * fh * t + ph)
        nL = dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)
        nR = dsp.dbfs_to_amp(-96.0) * rng.standard_normal(N)
        x[:, 0] += nL
        x[:, 1] += nR
        row = {"seed": seed}
        for ch, ci in (("L", 0), ("R", 1)):
            r = dsp.analyze_tone(x[:, ci], FS, pilot, N, nharm=2,
                                 band=(20.0, cal["band_hi"]),
                                 mains=cal["mains"])
            errs[f"prod_{ch}"].append((r["f0"] / pilot - 1) * 1e6 - PPM_TRUE)
            qc = est_lsq(x[:, ci], FS, pilot)
            f_est = qc.get("f_est")
            row[f"lsq_status_{ch}"] = qc.get("status", "edge_peak")
            row[f"f_est_hz_{ch}"] = (round(f_est, 4)
                                     if f_est is not None else None)
            row[f"line_hz_{ch}"] = qc.get("line_hz")
            if f_est is not None:
                errs[f"lsq_{ch}"].append(
                    (f_est / pilot - 1) * 1e6 - PPM_TRUE)
                row[f"err_ppm_pilot_{ch}"] = round(errs[f"lsq_{ch}"][-1], 3)
            else:
                row[f"err_ppm_pilot_{ch}"] = None
        raw.append(row)
    def st(v):
        a = np.asarray(v, dtype=float)
        return {"bias_ppm": round(float(np.mean(a)), 3),
                "sigma_ppm": round(float(np.std(a)), 3),
                "max_abs_ppm": round(float(np.max(np.abs(a))), 3)}
    f_l = [r["f_est_hz_L"] for r in raw if r["f_est_hz_L"] is not None]
    f_r = [r["f_est_hz_R"] for r in raw if r["f_est_hz_R"] is not None]
    st_l = sorted({r["lsq_status_L"] for r in raw})
    st_r = sorted({r["lsq_status_R"] for r in raw})
    return {"pilot_hz": pilot, "mode": mode, "trials": trials,
            # ПОЛНЫЙ счётчик по всем наблюдаемым статусам QC (в v2.18.4
            # считались только ok/edge_peak — line_contaminated был
            # невидим в сводке при 100% срабатываний на белом шуме)
            "prod_centroid_L": st(errs["prod_L"]),
            "prod_centroid_R": st(errs["prod_R"]),
            "lsq_L": st(errs["lsq_L"]), "lsq_R": st(errs["lsq_R"]),
            "f_est_min_hz_L": round(min(f_l), 3) if f_l else None,
            "f_est_max_hz_L": round(max(f_l), 3) if f_l else None,
            "f_est_min_hz_R": round(min(f_r), 3) if f_r else None,
            "f_est_max_hz_R": round(max(f_r), 3) if f_r else None,
            "lsq_status_counts_L": {s: sum(1 for r in raw
                                           if r["lsq_status_L"] == s)
                                    for s in st_l},
            "lsq_status_counts_R": {s: sum(1 for r in raw
                                           if r["lsq_status_R"] == s)
                                    for s in st_r},
            "per_trial": raw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="/opt/MyApps/Adudio/"
                    "ValveBench-v214-clock-diagnostic/measurements/"
                    "diagnostics/v214-clock-diagnostic")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    profile, n_win = measured_harmonic_profile(args.source_dir)
    print(f"профиль гармоник из WAV ({n_win} тихих окон): "
          f"линий с амплитудой > 0: "
          f"{sum(1 for v in profile.values() if v['amp_L'] > 0)}", flush=True)
    script_hash = hashlib.sha256(open(Path(__file__).resolve(), "rb").read()).hexdigest()
    print(f"script sha256: {script_hash[:20]}…", flush=True)
    rows = []
    cond_no = 0
    print("=== РЕАЛИСТИЧНЫЙ профиль гармоник (из WAV, кросс-фаза) ===",
          flush=True)
    for pilot in PILOTS:
        r = run(pilot, "measured", profile, cal, cond_no=cond_no)
        rows.append(r)
        cond_no += 1
        print(f"пилот {pilot:.0f}: "
              f"prod L {r['prod_centroid_L']['bias_ppm']:+6.2f}/"
              f"{r['prod_centroid_L']['sigma_ppm']:5.2f} R "
              f"{r['prod_centroid_R']['bias_ppm']:+6.2f}/"
              f"{r['prod_centroid_R']['sigma_ppm']:5.2f} | "
              f"LSQ L {r['lsq_L']['bias_ppm']:+6.2f}/"
              f"{r['lsq_L']['sigma_ppm']:5.2f} R "
              f"{r['lsq_R']['bias_ppm']:+6.2f}/"
              f"{r['lsq_R']['sigma_ppm']:5.2f} | "
              f"статусы L {r['lsq_status_counts_L']} "
              f"R {r['lsq_status_counts_R']}", flush=True)
    print("=== СТРЕСС-ТЕСТ: равномерная гребёнка −60 дБFS × 40 ===", flush=True)
    for pilot in PILOTS:
        r = run(pilot, "uniform_comb", profile, cal, trials=50, cond_no=cond_no)
        rows.append(r)
        cond_no += 1
        print(f"пилот {pilot:.0f} (гребёнка): "
              f"prod L {r['prod_centroid_L']['bias_ppm']:+6.2f}/"
              f"{r['prod_centroid_L']['sigma_ppm']:5.2f} R "
              f"{r['prod_centroid_R']['bias_ppm']:+6.2f}/"
              f"{r['prod_centroid_R']['sigma_ppm']:5.2f} | "
              f"LSQ L {r['lsq_L']['bias_ppm']:+6.2f}/"
              f"{r['lsq_L']['sigma_ppm']:5.2f} R "
              f"{r['lsq_R']['bias_ppm']:+6.2f}/"
              f"{r['lsq_R']['sigma_ppm']:5.2f}", flush=True)
    json.dump({"script_sha256": script_hash, "level_dbfs": LEVEL,
               "ppm_true": PPM_TRUE, "trials": TRIALS,
               "measured_profile": profile, "results": rows},
              open(OUT / "v2185-lsq-pilots.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2185-lsq-pilots.json")


if __name__ == "__main__":
    main()
