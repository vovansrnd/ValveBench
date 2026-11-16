#!/usr/bin/env python3
"""v2.18.5 — MC с line-aware оценщиком (две модели, 500 прогонов на
уровень). Оценщик возвращает словарь QC: статус (ok/edge_peak/
grid_edge/line_contaminated), coarse-позицию, сильнейшую линию остатка,
локальный SNR. Пер-trial CSV: seed, модель, ppm, ошибки, статус, линии.
Playback НЕТ; production только импортируется."""
import argparse
import csv
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
from v2185_estimators import est_lsq, est_joint  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS_MC = 48000.0
N_MC = int(2.0 * FS_MC)
TRIALS_PER_BATCH = 25
LEVELS = [-72.0, -66.0, -60.0]
PPM_TRUE = -23.5
BASE_SEED = 218900
NOISE_L_DBFS = -80.0
NOISE_R_DBFS = -96.0
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


def noise_white_corr(n, seed):
    rng = np.random.default_rng(seed)
    g = 10 ** (-16.0 / 20.0)
    vL = (dsp.dbfs_to_amp(NOISE_L_DBFS) / math.sqrt(2.0)) ** 2
    vR = (dsp.dbfs_to_amp(NOISE_R_DBFS) / math.sqrt(2.0)) ** 2
    sc2 = 0.97 * math.sqrt(vL * vR) / g
    siL2 = max(vL - sc2, 1e-30)
    siR2 = max(vR - sc2 * g * g, 1e-30)
    common = math.sqrt(sc2) * rng.standard_normal(n)
    nL = common + math.sqrt(siL2) * rng.standard_normal(n)
    nR = g * common + math.sqrt(siR2) * rng.standard_normal(n)
    return nL, nR


def quiet_windows(cap):
    w = int(0.05 * 192000)
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
                    s = int((i * 0.05 + k * 0.25) * 192000)
                    out.append((s, s + int(0.25 * 192000)))
            i = j
        else:
            i += 1
    return out


def measured_spectra(source_dir, fs_syn):
    nper = 8192
    S_LL = S_RR = S_LR = None
    n_win = 0
    for idx in IDX:
        _, cap = wavfile.read(str(Path(source_dir) / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        for (a, b) in quiet_windows(cap):
            f, P_LL = welch(cap[a:b, 0], 192000, nperseg=nper)
            _, P_RR = welch(cap[a:b, 1], 192000, nperseg=nper)
            _, P_LR = csd(cap[a:b, 0], cap[a:b, 1], 192000, nperseg=nper)
            S_LL = P_LL if S_LL is None else S_LL + P_LL
            S_RR = P_RR if S_RR is None else S_RR + P_RR
            S_LR = P_LR if S_LR is None else S_LR + P_LR
            n_win += 1
    S_LL /= n_win; S_RR /= n_win; S_LR /= n_win
    n = int(2.0 * fs_syn)
    f_t = np.arange(n // 2 + 1).astype(np.float64) * (fs_syn / n)
    S_LL_t = np.interp(f_t, f, S_LL)
    S_RR_t = np.interp(f_t, f, S_RR)
    S_LR_t = (np.interp(f_t, f, np.real(S_LR)) +
              1j * np.interp(f_t, f, np.imag(S_LR)))
    return f_t, S_LL_t, S_RR_t, S_LR_t, n_win


def noise_colored(n, seed, S_LL, S_RR, S_LR):
    rng = np.random.default_rng(seed)
    kernel = np.ones(41) / 41.0
    S_LL_s = np.convolve(S_LL, kernel, mode="same")
    S_RR_s = np.convolve(S_RR, kernel, mode="same")
    S_LR_s = np.convolve(S_LR, kernel, mode="same")
    g2_model = np.clip(np.abs(S_LR_s) ** 2 /
                       np.maximum(S_LL_s * S_RR_s, 1e-30), 0.0, 0.995)
    S_LR_model = np.sqrt(g2_model * S_LL_s * S_RR_s) * \
        np.exp(1j * np.angle(S_LR_s))
    # СОПРЯЖЕНИЕ (находка v2.18.5 verify): <nL·nR*> = conj(g)·S_LL, поэтому
    # для <nL·nR*> = S_LR требуется g = conj(S_LR)/S_LL; без сопряжения
    # фаза кросс-спектра зеркалится (verify: syn −φ vs model +φ).
    # На перканальные оценки не влияет (маргиналы и cos фазы чётны).
    gR_c = np.conj(S_LR_model) / np.maximum(S_LL_s, 1e-30)
    S_iR = S_RR_s * (1.0 - g2_model)

    def gauss_half():
        h = (rng.standard_normal(len(S_LL)) +
             1j * rng.standard_normal(len(S_LL))) / math.sqrt(2)
        h[0] = rng.standard_normal()
        h[-1] = rng.standard_normal()
        return h
    Wc = gauss_half()
    Wi = gauss_half()
    fs_c = FS_MC
    C_spec = np.sqrt(S_LL_s * fs_c * n / 2.0) * Wc
    I_spec = np.sqrt(S_iR * fs_c * n / 2.0) * Wi
    n_L = np.fft.irfft(C_spec, n)
    n_R = np.fft.irfft(gR_c * C_spec + I_spec, n)
    return n_L, n_R


def run_batch(model, batch_idx, trials_per_batch, cal, spectra):
    N_M = N_MC
    csv_path = OUT / f"v2185-trials-{model}-batch-{batch_idx:02d}.csv"
    rows = []
    for li, level in enumerate(LEVELS):
        for tr in range(trials_per_batch):
            seed = BASE_SEED + li * 100000 + batch_idx * trials_per_batch + tr
            rng = np.random.default_rng(seed)
            if model == "white":
                nL, nR = noise_white_corr(N_M, seed)
            else:
                nL, nR = noise_colored(N_M, seed, spectra[1], spectra[2],
                                       spectra[3])
            phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
            t = np.arange(N_M) / FS_MC
            f_true = 1000.0 * (1 + PPM_TRUE * 1e-6)
            tone = dsp.dbfs_to_amp(level) * np.sin(2 * math.pi * f_true * t +
                                                   phase0)
            L = tone + nL
            R = tone + nR
            row = {"seed": seed, "level_dbfs": level, "model": model}
            rLp = dsp.analyze_tone(L, FS_MC, 1000.0, N_M, nharm=2,
                                   band=(20.0, cal["band_hi"]),
                                   mains=cal["mains"])
            rRp = dsp.analyze_tone(R, FS_MC, 1000.0, N_M, nharm=2,
                                   band=(20.0, cal["band_hi"]),
                                   mains=cal["mains"])
            row["ppm_prod_L"] = (rLp["f0"] / 1000.0 - 1) * 1e6
            row["ppm_prod_R"] = (rRp["f0"] / 1000.0 - 1) * 1e6
            row["ppm_prod_mean"] = (row["ppm_prod_L"] + row["ppm_prod_R"]) / 2
            qcL = est_lsq(L, FS_MC, 1000.0)
            qcR = est_lsq(R, FS_MC, 1000.0)
            row["ppm_lsq_L"] = (qcL["f_est"] / 1000.0 - 1) * 1e6
            row["ppm_lsq_R"] = (qcR["f_est"] / 1000.0 - 1) * 1e6
            row["status_lsq_L"] = qcL["status"]
            row["status_lsq_R"] = qcR["status"]
            row["line_hz_L"] = qcL.get("line_hz")
            row["line_hz_R"] = qcR.get("line_hz")
            jg = est_joint(np.column_stack([L, R]), FS_MC, 1000.0)
            # FAIL-CLOSED (ревизия v2.18.4 п.3): jg = (f, var_f, fs, status);
            # при отказе старта f=None → ppm/sigma None, статус в CSV
            row["status_joint_gls"] = jg[3]
            row["ppm_joint_gls"] = ((jg[0] / 1000.0 - 1) * 1e6
                                    if jg[0] is not None else None)
            row["sigma_pred_joint_ppm"] = (
                math.sqrt(2.0 * jg[1]) / 1000.0 * 1e6
                if jg[1] is not None else None)
            for k in ("prod_L", "prod_R", "prod_mean", "lsq_L", "lsq_R",
                      "joint_gls"):
                row[f"err_{k}"] = row[f"ppm_{k}"] - PPM_TRUE
            rows.append(row)
            del L, R, nL, nR, tone
            if tr % 25 == 0:
                import gc
                gc.collect()
    keys = list(rows[0].keys())
    with open(csv_path, "w") as fh:
        fh.write(",".join(keys) + "\n")
        for row in rows:
            fh.write(",".join(str(row[k]) for k in keys) + "\n")
    print(f"батч {model}/{batch_idx}: {len(rows)} строк", flush=True)


def aggregate(model):
    rows = []
    for p in sorted(OUT.glob(f"v2185-trials-{model}-batch-*.csv")):
        with open(p) as fh:
            for r in csv.DictReader(fh):
                out_r = {}
                for k, v in r.items():
                    if k == "seed":
                        out_r[k] = int(v)
                    elif k in ("model", "status_lsq_L", "status_lsq_R",
                               "status_joint_gls"):
                        out_r[k] = v
                    else:
                        try:
                            out_r[k] = float(v)
                        except (ValueError, TypeError):
                            out_r[k] = float("nan") if v == "None" else v
                rows.append(out_r)
    summary = {"model": model, "levels": [], "status_counts": {}}
    for level in LEVELS:
        sel = [r for r in rows if r["level_dbfs"] == level]
        rec = {"level_dbfs": level, "trials": len(sel), "estimators": {}}
        for k in ("prod_L", "prod_R", "prod_mean", "lsq_L", "lsq_R",
                  "joint_gls"):
            a = np.asarray([r[f"err_{k}"] for r in sel])
            rec["estimators"][k] = {
                "bias_ppm": round(float(np.mean(a)), 3),
                "sigma_ppm": round(float(np.std(a)), 3),
                "max_abs_ppm": round(float(np.max(np.abs(a))), 3)}
        st_l = [r["status_lsq_L"] for r in sel]
        st_r = [r["status_lsq_R"] for r in sel]
        rec["status_counts"] = {
            "L": {s: st_l.count(s) for s in set(st_l)},
            "R": {s: st_r.count(s) for s in set(st_r)}}
        st_j = [r.get("status_joint_gls") or "не_записано" for r in sel]
        rec["joint_status_counts"] = {s: st_j.count(s) for s in set(st_j)}
        lines_hz_L = [r["line_hz_L"] for r in sel
                      if isinstance(r.get("line_hz_L"), float) and
                      np.isfinite(r["line_hz_L"])]
        lines_hz_R = [r["line_hz_R"] for r in sel
                      if isinstance(r.get("line_hz_R"), float) and
                      np.isfinite(r["line_hz_R"])]
        rec["lines_detected_hz"] = {"L": sorted(set(lines_hz_L)),
                                    "R": sorted(set(lines_hz_R))}
        sp = np.asarray([r["sigma_pred_joint_ppm"] for r in sel
                         if np.isfinite(r["sigma_pred_joint_ppm"])])
        je = np.asarray([r["err_joint_gls"] for r in sel
                         if np.isfinite(r["sigma_pred_joint_ppm"])])
        spv = sp[np.isfinite(sp)]
        mean_sp = float(np.mean(spv))
        rec["sigma_pred_stats"] = {
            "n_valid": int(len(spv)),
            "mean_pred_ppm": round(mean_sp, 3),
            "actual_sigma_ppm": round(float(np.std(je)), 3),
            "pred_over_actual": round(mean_sp / max(float(np.std(je)),
                                                    1e-9), 3),
            "coverage_1sigma_per_trial": round(float(np.mean(
                np.abs(je) <= spv)), 3),
            "coverage_2sigma_per_trial": round(float(np.mean(
                np.abs(je) <= 2 * spv)), 3)}
        summary["levels"].append(rec)
        summary["status_counts"][f"{level:+.0f}"] = rec["status_counts"]
        e = rec["estimators"]
        st = rec["sigma_pred_stats"]
        print(f"{model:8s} {level:+6.0f} (n={len(sel)}): "
              f"prod L σ={e['prod_L']['sigma_ppm']:.2f} "
              f"R σ={e['prod_R']['sigma_ppm']:.2f} | "
              f"lsq L σ={e['lsq_L']['sigma_ppm']:.2f} "
              f"R σ={e['lsq_R']['sigma_ppm']:.2f} | "
              f"jGLS σ={e['joint_gls']['sigma_ppm']:.2f} | "
              f"статусы L {rec['status_counts']['L']} "
              f"R {rec['status_counts']['R']}", flush=True)
    return summary, rows


def verify_models(f_t, S_LL, S_RR, S_LR, n_check=15):
    rng = np.random.default_rng(BASE_SEED + 903)
    n = int(2.0 * FS_MC)
    bins = np.arange(n // 2 + 1).astype(np.float64) * (FS_MC / n)
    aLL = np.zeros(len(bins))
    aRR = np.zeros(len(bins))
    aLR = np.zeros(len(bins), dtype=complex)
    phL = np.zeros(len(bins), dtype=complex)
    for i in range(n_check):
        nL, nR = noise_colored(n, BASE_SEED + 904 + i, S_LL, S_RR, S_LR)
        FL = np.fft.rfft(nL)
        FR = np.fft.rfft(nR)
        aLL += np.abs(FL) ** 2 * 2.0 / (FS_MC * n) / n_check
        aRR += np.abs(FR) ** 2 * 2.0 / (FS_MC * n) / n_check
        aLR += FL * np.conj(FR) * 2.0 / (FS_MC * n) / n_check
        phL += FL * 2.0 / (FS_MC * n) / n_check
    out = []
    for lo, hi in ((900, 1100), (100, 500), (500, 2000), (2000, 8000),
                   (8000, 24000)):
        mb = (bins >= lo) & (bins <= hi)
        g2_syn = float(np.mean(np.abs(aLR[mb]) ** 2 /
                               np.maximum(aLL[mb] * aRR[mb], 1e-30)))
        dL = float(np.mean(10 * np.log10(aLL[mb] / np.maximum(S_LL[mb], 1e-30))))
        dR = float(np.mean(10 * np.log10(aRR[mb] / np.maximum(S_RR[mb], 1e-30))))
        ph_syn = float(np.mean(np.angle(aLR[mb])))
        ph_mod = float(np.mean(np.angle(S_LR[mb])))
        out.append({"band_hz": [lo, hi],
                    "gamma2_synthesized": round(g2_syn, 3),
                    "psd_L_dev_db": round(dL, 2), "psd_R_dev_db": round(dR, 2),
                    "cross_phase_syn_rad": round(ph_syn, 3),
                    "cross_phase_model_rad": round(ph_mod, 3)})
        print(f"    [{lo:>5}-{hi:<5}] Гц: γ² {g2_syn:.3f} | PSD L {dL:+.2f} "
              f"R {dR:+.2f} дБ | фаза {ph_syn:+.3f}/{ph_mod:+.3f} рад", flush=True)
    return {"bands": out, "check_trials": n_check}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="/opt/MyApps/Adudio/"
                    "ValveBench-v214-clock-diagnostic/measurements/"
                    "diagnostics/v214-clock-diagnostic")
    ap.add_argument("--model", choices=("white", "colored"), default="white")
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--trials-per-batch", type=int, default=25)
    ap.add_argument("--aggregate", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    spectra = None
    if args.model == "colored":
        spectra = measured_spectra(args.source_dir, FS_MC)
        print(f"COLORED: модель из {spectra[4]} тихих окон", flush=True)
    if args.aggregate:
        summary, _ = aggregate(args.model)
        json.dump(summary, open(OUT / f"v2185-montecarlo-{args.model}-summary.json",
                                "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"JSON: v2185-montecarlo-{args.model}-summary.json", flush=True)
        return
    if args.batch is None:
        if args.model == "colored":
            ver = verify_models(*spectra)
            json.dump(ver, open(OUT / "v2185-verify-colored.json", "w",
                                encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print(json.dumps(ver, ensure_ascii=False)[:400], flush=True)
        return
    run_batch(args.model, args.batch, args.trials_per_batch, cal, spectra)


if __name__ == "__main__":
    main()
