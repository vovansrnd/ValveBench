#!/usr/bin/env python3
"""v2.18.7 — пилоты с КАЛИБРОВАННЫМ извлечением мощности линий
(журнал §20.7 п.4) и оценкой v2.18.6 на обоих каналах.

Дефект v2.18.5 (ревизия): nperseg 8192 при 192 кГц даёт шаг Welch
23.4375 Гц; лепесток ±12 Гц захватывал 1 бин ≈ 58–65% мощности линии
(−1.9…−2.4 дБ). Здесь:
  * nperseg = 32768 (шаг 5.859 Гц): главный лепесток Ханна линии
    (±11.7 Гц) целиком внутри лепестка ±12 Гц;
  * КАЛИБРОВКА на внесённых линиях: для каждой линии 50·k синтезируется
    чистая синусоида известной амплитуды, прогоняется ТОТ ЖЕ конвейер
    (welch → лепесток → Σ S·df) → доля восстановления ρ(fh);
  * вычитание фона из боковых колец ±[16, 34] Гц (между линиями);
  * ВАЛИДАЦИЯ: линии с восстановленными амплитудами вносятся в шум,
    извлекаются обратно, ошибка в дБ (цель ≤ 0.5 дБ);
  * кросс-фаза линии из центральных бинов CSD (фаза R относительно L);
  * хэши SHA256 исходных WAV и скрипта записываются в JSON.

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
from v2187_estimators import est_lsq  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS_CAP = 192000
FS = 48000.0
N = int(2.0 * FS)
TRIALS = 100
LEVEL = -72.0
PPM_TRUE = -23.5
BASE_SEED = 218700
PILOTS = (997.0, 1000.0, 1013.0)
NPERSEG = 32768                 # шаг 5.859 Гц: главный лепесток ±11.7 Гц
PETAL_HZ = 12.0
RING_LO, RING_HI = 16.0, 34.0   # боковые кольца для фона (между линиями)
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
CAL_AMP = dsp.dbfs_to_amp(-60.0)   # амплитуда калибровочных линий


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


def line_power_from_psd(f, S, fh):
    """Мощность линии fh из PSD: лепесток ±12 Гц минус фон боковых колец."""
    df = float(f[1] - f[0])
    m = (f >= fh - PETAL_HZ) & (f <= fh + PETAL_HZ)
    ring = ((f >= fh - RING_HI) & (f <= fh - RING_LO)) | \
           ((f >= fh + RING_LO) & (f <= fh + RING_HI))
    bg = float(np.median(S[ring])) if ring.any() else 0.0
    return max(float(np.sum(S[m]) * df) - bg * (2 * PETAL_HZ), 1e-30)


def pipeline_psd(seg, fs):
    """PSD одного окна — ТОТ ЖЕ конвейер, что и для захватов."""
    f, P = welch(seg, fs, nperseg=NPERSEG)
    return f, P


def calibrate_recovery(fh_list):
    """ρ(fh): доля мощности внесённой линии, захваченная конвейером
    (welch nperseg 32768 → лепесток ±12 Гц → Σ S·df − фон)."""
    n_win = int(0.25 * FS_CAP)
    t = np.arange(n_win) / FS_CAP
    rho = {}
    for fh in fh_list:
        x = CAL_AMP * np.sin(2 * math.pi * fh * t + 0.31)
        f, P = pipeline_psd(x, FS_CAP)
        P_line = line_power_from_psd(f, P, fh)
        P_true = CAL_AMP ** 2 / 2.0
        rho[fh] = P_line / P_true
    return rho


def measured_harmonic_profile(source_dir):
    """Профиль гармоник ИЗ WAV: усреднённые PSD/CSD тихих окон,
    мощность линии = (лепесток − фон) / ρ(fh); пиковая амплитуда
    A = √(2·P); кросс-фаза из центральных бинов CSD."""
    S_LL = S_RR = S_LR = None
    n_win = 0
    wav_hashes = {}
    for idx in IDX:
        path = Path(source_dir) / CAPTURES[idx]
        data = path.read_bytes()
        wav_hashes[CAPTURES[idx]] = hashlib.sha256(data).hexdigest()
        _, cap = wavfile.read(str(path))
        cap = cap.astype(np.float64)
        for (a, b) in quiet_windows(cap):
            f, P_LL = welch(cap[a:b, 0], FS_CAP, nperseg=NPERSEG)
            _, P_RR = welch(cap[a:b, 1], FS_CAP, nperseg=NPERSEG)
            _, P_LR = csd(cap[a:b, 0], cap[a:b, 1], FS_CAP, nperseg=NPERSEG)
            S_LL = P_LL if S_LL is None else S_LL + P_LL
            S_RR = P_RR if S_RR is None else S_RR + P_RR
            S_LR = P_LR if S_LR is None else S_LR + P_LR
            n_win += 1
    S_LL /= n_win; S_RR /= n_win; S_LR /= n_win
    df = float(f[1] - f[0])
    fh_list = [50.0 * k for k in range(1, 41) if 50.0 * k < FS_CAP / 2 - 50]
    rho = calibrate_recovery(fh_list)
    profile = {}
    for fh in fh_list:
        m = (f >= fh - PETAL_HZ) & (f <= fh + PETAL_HZ)
        ring = ((f >= fh - RING_HI) & (f <= fh - RING_LO)) | \
               ((f >= fh + RING_LO) & (f <= fh + RING_HI))
        bgL = float(np.median(S_LL[ring])) if ring.any() else 0.0
        bgR = float(np.median(S_RR[ring])) if ring.any() else 0.0
        pL = max(float(np.sum(S_LL[m]) * df) - bgL * (2 * PETAL_HZ), 1e-30)
        pR = max(float(np.sum(S_RR[m]) * df) - bgR * (2 * PETAL_HZ), 1e-30)
        mc = (f >= fh - 6.0) & (f <= fh + 6.0)
        s_lr_mean = complex(np.mean(S_LR[mc])) if mc.any() else 0j
        phase_lr = round(math.atan2(s_lr_mean.imag, s_lr_mean.real), 4)
        profile[fh] = {"amp_L": round(math.sqrt(2 * pL / rho[fh]), 8),
                       "amp_R": round(math.sqrt(2 * pR / rho[fh]), 8),
                       "rho": round(rho[fh], 4),
                       "phase_LR_rad": phase_lr}
    return profile, n_win, rho, wav_hashes


def validate_calibration(profile, n_win=40):
    """Валидация: линии профиля вносятся в белый шум −80 дБFS (тот же
    конвейер) → извлечение → ошибка в дБ против внесённой амплитуды."""
    errs = []
    rng = np.random.default_rng(BASE_SEED + 777)
    for k in range(1, 41):
        fh = 50.0 * k
        if fh not in profile or profile[fh]["amp_L"] <= 0:
            continue
        a = profile[fh]["amp_L"]
        seg = dsp.dbfs_to_amp(-80.0) / math.sqrt(2.0) * \
            rng.standard_normal(int(0.25 * FS_CAP))
        t = np.arange(int(0.25 * FS_CAP)) / FS_CAP
        seg = seg + a * np.sin(2 * math.pi * fh * t +
                               2 * math.pi * rng.random())
        f, P = pipeline_psd(seg, FS_CAP)
        df = float(f[1] - f[0])
        m = (f >= fh - PETAL_HZ) & (f <= fh + PETAL_HZ)
        ring = ((f >= fh - RING_HI) & (f <= fh - RING_LO)) | \
               ((f >= fh + RING_LO) & (f <= fh + RING_HI))
        bg = float(np.median(P[ring])) if ring.any() else 0.0
        P_meas = max(float(np.sum(P[m]) * df) - bg * (2 * PETAL_HZ), 1e-30)
        a_rec = math.sqrt(2 * P_meas / profile[fh]["rho"])
        errs.append(20 * math.log10(a_rec / a))
    return {"n_lines": len(errs), "err_db_max": round(max(abs(e) for e in errs), 2),
            "err_db_mean_abs": round(float(np.mean(np.abs(errs))), 2),
            "per_line_db": [round(e, 2) for e in errs]}


def run(pilot, mode, profile, cal, trials=TRIALS, cond_no=0):
    errs = {"prod_L": [], "prod_R": [], "lsq_L": [], "lsq_R": []}
    raw = []
    for tr in range(trials):
        seed = BASE_SEED + int(pilot) * 100 + cond_no * 10 + tr
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
                x[:, 0] += a["amp_L"] * np.sin(2 * math.pi * fh * t + ph)
                x[:, 1] += a["amp_R"] * np.sin(2 * math.pi * fh * t + ph +
                                               a["phase_LR_rad"])
            else:
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
            row[f"lsq_status_{ch}"] = qc.get("status", "?")
            row[f"line_hz_{ch}"] = qc.get("line_hz")
            row[f"line_dbc_{ch}"] = qc.get("line_dbc")
            row[f"snr_carrier_db_{ch}"] = qc.get("snr_carrier_db")
            row[f"f_est_hz_{ch}"] = (round(f_est, 4)
                                     if f_est is not None else None)
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
            "prod_centroid_L": st(errs["prod_L"]),
            "prod_centroid_R": st(errs["prod_R"]),
            "lsq_L": st(errs["lsq_L"]), "lsq_R": st(errs["lsq_R"]),
            "f_est_min_hz_L": round(min(f_l), 3) if f_l else None,
            "f_est_max_hz_L": round(max(f_l), 3) if f_l else None,
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
    profile, n_win, rho, wav_hashes = measured_harmonic_profile(args.source_dir)
    print(f"профиль гармоник из WAV ({n_win} тихих окон, nperseg "
          f"{NPERSEG}): линий >0: "
          f"{sum(1 for v in profile.values() if v['amp_L'] > 0)}", flush=True)
    rho_vals = [v["rho"] for v in profile.values()]
    print(f"ρ(fh): min {min(rho_vals):.3f} max {max(rho_vals):.3f} "
          f"(дефект v2.18.5: 0.58–0.65)", flush=True)
    val = validate_calibration(profile)
    print(f"ВАЛИДАЦИЯ извлечения: {val['n_lines']} линий, "
          f"|err| ср {val['err_db_mean_abs']} дБ, макс "
          f"{val['err_db_max']} дБ", flush=True)
    script_hash = hashlib.sha256(open(Path(__file__).resolve(), "rb").read()).hexdigest()
    # provenance (§21 п.6): SHA256 оценщика и WAV-манифеста
    est_hash = hashlib.sha256(
        (Path(__file__).resolve().parent / "v2187_estimators.py").read_bytes()
    ).hexdigest()
    manifest_path = Path(__file__).resolve().parent / "wav" / "MANIFEST.json"
    manifest_hash = (hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                     if manifest_path.exists() else None)
    rows = []
    cond_no = 0
    print("=== РЕАЛИСТИЧНЫЙ профиль гармоник (из WAV, калиброванный) ===",
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
    print("=== СТРЕСС-ТЕСТ: равномерная гребёнка −60 дБFS × 40 ===",
          flush=True)
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
    json.dump({"script_sha256": script_hash,
               "estimator_sha256": est_hash,
               "estimator_module": "v2187_estimators.py",
               "wav_manifest_sha256": manifest_hash,
               "level_dbfs": LEVEL,
               "ppm_true": PPM_TRUE, "trials": TRIALS,
               "nperseg": NPERSEG, "petal_hz": PETAL_HZ,
               "measured_profile": profile, "rho_min_max":
               [round(min(rho_vals), 4), round(max(rho_vals), 4)],
               "validation": val,
               "source_wav_sha256": wav_hashes, "results": rows},
              open(OUT / "v2187-lsq-pilots.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2187-lsq-pilots.json")


if __name__ == "__main__":
    main()
