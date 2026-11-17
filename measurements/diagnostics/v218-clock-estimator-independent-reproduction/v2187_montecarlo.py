#!/usr/bin/env python3
"""v2.18.6 — MC с калиброванным QC и ПРАВИЛЬНОЙ конвенцией CSD
(ревизия v2.18.5, журнал §20.7 п.1/5/6).

Конвенция CSD: scipy.signal.csd(L, R) = conj(FFT(L))·FFT(R).
Для синтеза R = g·L с измеренным кросс-спектром: <conj(L)·R> = g·S_LL,
т.е. g = S_LR/S_LL БЕЗ сопряжения. Verifier считает conj(FL)·FR — та же
конвенция, что и csd. (В v2.18.5 стояло conj(g) при verifier FL·conj(FR):
два взаимно компенсирующих смены знака — совпадение было кажущимся;
истинная фаза синтезированной пары была −φ. Проверено численно на
синусоидах с известным сдвигом +0.37 рад — v2186_unit_tests.py.)

Цветная модель — ШУМОВОЙ ПОЛ захватов БЕЗ дискретных линий (линии сети
удаляются из PSD данных; они моделируются отдельно как интерференция и
обязаны ловиться QC как TP — v2186_qc_confusion.py).

Агрегация разделяет ПРИНЯТЫЕ (status ok) и ОТВЕРГНУТЫЕ оценки: ошибки
частоты публикуются по принятым; отвергнутые — отдельно (статусы,
кластеры line_hz, какими были бы ошибки без QC).
Playback НЕТ; production только импортируется.
"""
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
from v2187_estimators import est_lsq, est_joint  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS_MC = 48000.0
N_MC = int(2.0 * FS_MC)
TRIALS_PER_BATCH = 25
LEVELS = [-72.0, -66.0, -60.0]
PPM_TRUE = -23.5
BASE_SEED = 218600
NOISE_L_DBFS = -80.0
NOISE_R_DBFS = -96.0
IDXL = [1, 2, 15, 16, 3, 4, 8]
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


def flatten_model_peaks(f, S, comb_hz=(50.0 * k for k in range(1, 41)),
                        petal_hz=15.0, peak_db=5.0, floor_bins=201):
    """Удаление дискретных пиков из PSD модели (ревизия §20.7 п.1):
    цветная модель = гладкий шумовой пол; узкие пики (линии сети ±15 Гц
    И любые бины > peak_db над локальной медианой) заменяются локальным
    фоном. Возвращает уплощённую копию и число заменённых бинов."""
    f = np.asarray(f)
    S = np.asarray(S, dtype=np.complex128 if np.iscomplexobj(S) else np.float64)
    is_c = np.iscomplexobj(S)
    mag = np.abs(S) if is_c else S
    floor_loc = median_filter_mag(mag, floor_bins)
    peak = mag > 10 ** (peak_db / 20.0) * floor_loc
    comb = np.zeros(len(f), dtype=bool)
    for fh in comb_hz:
        if fh < f[-1]:
            comb |= np.abs(f - fh) <= petal_hz
    sel = peak | comb
    sel &= f > 5.0
    n_rep = int(sel.sum())
    if n_rep == 0:
        return S, 0
    rep = floor_loc[sel]
    if is_c:
        ph = np.angle(S[sel])
        S = S.copy()
        S[sel] = rep * np.exp(1j * ph)
    else:
        S = S.copy()
        S[sel] = rep
    return S, n_rep


def median_filter_mag(mag, size):
    from scipy.ndimage import median_filter
    return median_filter(mag, size=size, mode="nearest")


def measured_spectra(source_dir, fs_syn):
    """PSD/кросс-спектр тихих окон захватов; дискретные линии УДАЛЕНЫ
    (шумовая модель без интерференции). Возвращает также счётчик
    заменённых бинов и запись об исходных WAV."""
    nper = 8192
    S_LL = S_RR = S_LR = None
    n_win = 0
    for idx in IDXL:
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
    S_LL, nL = flatten_model_peaks(f, S_LL)
    S_RR, nR = flatten_model_peaks(f, S_RR)
    # кросс-спектр: амплитуда масштабируется к уплощённым PSD, фаза
    # сохраняется (когерентная мощность линий убрана вместе с ними)
    S_LR, nX = flatten_model_peaks(f, S_LR)
    n_t = int(2.0 * fs_syn)
    f_t = np.arange(n_t // 2 + 1).astype(np.float64) * (fs_syn / n_t)
    S_LL_t = np.interp(f_t, f, S_LL)
    S_RR_t = np.interp(f_t, f, S_RR)
    S_LR_t = (np.interp(f_t, f, np.real(S_LR)) +
              1j * np.interp(f_t, f, np.imag(S_LR)))
    info = {"n_windows": n_win, "n_bins_flattened": {"S_LL": nL, "S_RR": nR,
                                                     "S_LR": nX}}
    return f_t, S_LL_t, S_RR_t, S_LR_t, n_win, info


def noise_colored(n, seed, S_LL, S_RR, S_LR):
    """Синтез цветной пары. КОНВЕНЦИЯ (ревизия v2.18.5): csd(L, R) =
    conj(FFT(L))·FFT(R); R = g·L даёт <conj(L)·R> = g·S_LL, поэтому
    g = S_LR/S_LL БЕЗ сопряжения. Verifier — conj(FL)·FR (та же
    конвенция)."""
    rng = np.random.default_rng(seed)
    kernel = np.ones(41) / 41.0
    S_LL_s = np.convolve(S_LL, kernel, mode="same")
    S_RR_s = np.convolve(S_RR, kernel, mode="same")
    S_LR_s = np.convolve(S_LR, kernel, mode="same")
    g2_model = np.clip(np.abs(S_LR_s) ** 2 /
                       np.maximum(S_LL_s * S_RR_s, 1e-30), 0.0, 0.995)
    S_LR_model = np.sqrt(g2_model * S_LL_s * S_RR_s) * \
        np.exp(1j * np.angle(S_LR_s))
    gR_c = S_LR_model / np.maximum(S_LL_s, 1e-30)   # БЕЗ сопряжения
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
    csv_path = OUT / f"v2187-trials-{model}-batch-{batch_idx:02d}.csv"
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
            for ch, sig in (("L", L), ("R", R)):
                qc = est_lsq(sig, FS_MC, 1000.0)
                row[f"status_lsq_{ch}"] = qc["status"]
                row[f"line_hz_{ch}"] = qc.get("line_hz")
                row[f"line_dbc_{ch}"] = qc.get("line_dbc")
                fe = qc.get("f_est")
                row[f"ppm_lsq_{ch}"] = ((fe / 1000.0 - 1) * 1e6
                                        if fe is not None else None)
            jg = est_joint(np.column_stack([L, R]), FS_MC, 1000.0)
            row["status_joint_gls"] = jg["status"]
            row["channels_used_joint"] = "+".join(jg["channels_used"])
            row["ppm_joint_gls"] = ((jg["f_est"] / 1000.0 - 1) * 1e6
                                    if jg["f_est"] is not None else None)
            # §21 п.3: sigma_hz из оценщика — sqrt применён РОВНО ОДИН
            # раз (в v2.18.6 MC добавлял лишний sqrt(2·var))
            row["sigma_pred_joint_ppm"] = (
                jg["sigma_hz"] / 1000.0 * 1e6
                if jg["sigma_hz"] is not None else None)
            for k in ("prod_L", "prod_R", "prod_mean", "lsq_L", "lsq_R",
                      "joint_gls"):
                pv = row[f"ppm_{k}"]
                row[f"err_{k}"] = (pv - PPM_TRUE) if pv is not None else None
            rows.append(row)
            del L, R, nL, nR, tone
            if tr % 25 == 0:
                import gc
                gc.collect()
    keys = list(rows[0].keys())
    with open(csv_path, "w") as fh:
        fh.write(",".join(keys) + "\n")
        for row in rows:
            fh.write(",".join(
                ("" if row[k] is None else str(row[k])) for k in keys) + "\n")
    print(f"батч {model}/{batch_idx}: {len(rows)} строк", flush=True)


def _stat(vals):
    a = np.asarray([v for v in vals if v is not None and np.isfinite(v)],
                   dtype=float)
    if len(a) == 0:
        return {"n": 0, "bias_ppm": None, "sigma_ppm": None,
                "max_abs_ppm": None}
    return {"n": int(len(a)),
            "bias_ppm": round(float(np.mean(a)), 3),
            "sigma_ppm": round(float(np.std(a)), 3),
            "max_abs_ppm": round(float(np.max(np.abs(a))), 3)}


def aggregate(model):
    rows = []
    for p in sorted(OUT.glob(f"v2187-trials-{model}-batch-*.csv")):
        with open(p) as fh:
            for r in csv.DictReader(fh):
                out_r = {}
                for k, v in r.items():
                    if k == "seed":
                        out_r[k] = int(v)
                    elif k.startswith("status") or k == "model":
                        out_r[k] = v
                    elif v == "":
                        out_r[k] = None
                    else:
                        try:
                            out_r[k] = float(v)
                        except (ValueError, TypeError):
                            out_r[k] = None
                rows.append(out_r)
    summary = {"model": model, "levels": [], "status_counts": {}}
    for level in LEVELS:
        sel = [r for r in rows if r["level_dbfs"] == level]
        rec = {"level_dbfs": level, "trials": len(sel), "accepted": {},
               "rejected": {}}
        # ПРИНЯТЫЕ/ОТВЕРГНУТЫЕ раздельно per estimator (ревизия п.6)
        acc_l = [r for r in sel if r["status_lsq_L"] == "ok"]
        acc_r = [r for r in sel if r["status_lsq_R"] == "ok"]
        acc_j = [r for r in sel
                 if str(r.get("status_joint_gls") or "").startswith("ok")]
        rec["accepted"]["lsq_L"] = _stat([r["err_lsq_L"] for r in acc_l])
        rec["accepted"]["lsq_R"] = _stat([r["err_lsq_R"] for r in acc_r])
        rec["accepted"]["joint_gls"] = _stat(
            [r["err_joint_gls"] for r in acc_j])
        rec["accepted"]["counts"] = {
            "lsq_L": len(acc_l), "lsq_R": len(acc_r), "joint_gls": len(acc_j)}
        # production (центроид) — без QC, все прогоны (как есть в prod)
        rec["accepted"]["prod_L"] = _stat([r["err_prod_L"] for r in sel])
        rec["accepted"]["prod_R"] = _stat([r["err_prod_R"] for r in sel])
        # ОТВЕРГНУТЫЕ: статусы, кластеры line_hz, ошибки «как без QC»
        rej_l = [r for r in sel if r["status_lsq_L"] != "ok"]
        rej_r = [r for r in sel if r["status_lsq_R"] != "ok"]
        rej_j = [r for r in sel
                 if not str(r.get("status_joint_gls") or "").startswith("ok")]
        rec["rejected"]["counts"] = {
            "lsq_L": len(rej_l), "lsq_R": len(rej_r),
            "joint_gls": len(rej_j)}
        rec["rejected"]["status_counts"] = {
            "L": {s: sum(1 for r in rej_l if r["status_lsq_L"] == s)
                  for s in {r["status_lsq_L"] for r in rej_l}},
            "R": {s: sum(1 for r in rej_r if r["status_lsq_R"] == s)
                  for s in {r["status_lsq_R"] for r in rej_r}},
            "joint": {s: sum(1 for r in rej_j
                             if r.get("status_joint_gls") == s)
                      for s in {r.get("status_joint_gls") for r in rej_j}}}
        rec["rejected"]["raw_errors"] = {
            "lsq_L": _stat([r["err_lsq_L"] for r in rej_l]),
            "lsq_R": _stat([r["err_lsq_R"] for r in rej_r]),
            "joint_gls": _stat([r["err_joint_gls"] for r in rej_j])}
        lines = {}
        for r in rej_l + rej_r:
            for ch in ("L", "R"):
                hz = r.get(f"line_hz_{ch}")
                if hz is not None and np.isfinite(hz):
                    lines[round(hz, 1)] = lines.get(round(hz, 1), 0) + 1
        rec["rejected"]["line_hz_top"] = sorted(lines.items(),
                                                key=lambda t: -t[1])[:8]
        # σ_pred по принятым joint
        sp = [r["sigma_pred_joint_ppm"] for r in acc_j
              if r.get("sigma_pred_joint_ppm") is not None]
        je = [r["err_joint_gls"] for r in acc_j
              if r.get("sigma_pred_joint_ppm") is not None]
        spv = np.asarray([v for v in sp if np.isfinite(v)])
        jev = np.asarray([v for v in je if np.isfinite(v)])
        mean_sp = float(np.mean(spv)) if len(spv) else None
        rec["sigma_pred_stats"] = {
            "n_valid": int(len(spv)),
            "mean_pred_ppm": round(mean_sp, 3) if mean_sp is not None
            else None,
            "actual_sigma_ppm": (round(float(np.std(jev)), 3)
                                 if len(jev) else None),
            "pred_over_actual": (round(mean_sp / max(float(np.std(jev)),
                                                     1e-9), 3)
                                 if mean_sp is not None and len(jev) else None),
            "coverage_1sigma_per_trial": (round(float(np.mean(
                np.abs(jev) <= spv)), 3) if len(jev) else None),
            "coverage_2sigma_per_trial": (round(float(np.mean(
                np.abs(jev) <= 2 * spv)), 3) if len(jev) else None)}
        # confusion (в MC нет внесённой интерференции):
        # TN = оба канала ok; FP = line_contaminated без интерференции
        tn = sum(1 for r in sel if r["status_lsq_L"] == "ok"
                 and r["status_lsq_R"] == "ok")
        fp = sum(1 for r in sel if r["status_lsq_L"] == "line_contaminated"
                 or r["status_lsq_R"] == "line_contaminated")
        rec["confusion_negatives"] = {"true_negative": tn,
                                      "false_positive_line_qc": fp,
                                      "other_refusals": len(sel) - tn - fp}
        summary["levels"].append(rec)
        summary["status_counts"][f"{level:+.0f}"] = {
            "accepted_counts": rec["accepted"]["counts"],
            "rejected_counts": rec["rejected"]["counts"]}
        e = rec["accepted"]
        print(f"{model:8s} {level:+6.0f}: приняты L {e['counts']['lsq_L']}/"
              f"{len(sel)} R {e['counts']['lsq_R']} jGLS "
              f"{e['counts']['joint_gls']} | σ lsq L "
              f"{e['lsq_L']['sigma_ppm']} R {e['lsq_R']['sigma_ppm']} jGLS "
              f"{e['joint_gls']['sigma_ppm']} | prod L "
              f"{e['prod_L']['sigma_ppm']} R {e['prod_R']['sigma_ppm']} | "
              f"FP(line) {fp}", flush=True)
    return summary, rows


def verify_models(f_t, S_LL, S_RR, S_LR, n_check=15):
    """Верификация цветной модели (ревизия §20.7 п.1): конвенция verifier
    = conj(FL)·FR — ТА ЖЕ, что у scipy.signal.csd. PSD, γ², комплексная
    фаза кросс-спектра по полосам."""
    rng = np.random.default_rng(BASE_SEED + 903)
    n = int(2.0 * FS_MC)
    bins = np.arange(n // 2 + 1).astype(np.float64) * (FS_MC / n)
    aLL = np.zeros(len(bins))
    aRR = np.zeros(len(bins))
    aLR = np.zeros(len(bins), dtype=complex)
    for i in range(n_check):
        nL, nR = noise_colored(n, BASE_SEED + 904 + i, S_LL, S_RR, S_LR)
        FL = np.fft.rfft(nL)
        FR = np.fft.rfft(nR)
        aLL += np.abs(FL) ** 2 * 2.0 / (FS_MC * n) / n_check
        aRR += np.abs(FR) ** 2 * 2.0 / (FS_MC * n) / n_check
        aLR += np.conj(FL) * FR * 2.0 / (FS_MC * n) / n_check   # конвенция
    out = {"convention": "csd(L,R) = conj(FFT(L))·FFT(R) (scipy)",
            "known_phase_check": None, "bands": [], "check_trials": n_check}
    # known-phase: модель с постоянной фазой +0.37 рад → измеренная фаза
    ph_model = 0.37
    S_LR_ph = np.abs(S_LR) * np.exp(1j * ph_model)
    nL, nR = noise_colored(n, BASE_SEED + 950, S_LL, S_RR, S_LR_ph)
    FL = np.fft.rfft(nL); FR = np.fft.rfft(nR)
    mb = (bins >= 900) & (bins <= 1100)
    # PSD-нормировка та же, что у verifier: aLR = conj(FL)·FR·2/(fs·n)
    aLR_ph = np.conj(FL[mb]) * FR[mb] * 2.0 / (FS_MC * n)
    cross_mean = np.mean(aLR_ph)
    ph_meas = float(np.angle(cross_mean))
    # АМПЛИТУДНАЯ проверка (§21 п.6): |среднее aLR| против среднего
    # |S_LR_ph| — когерентная передача R от L той же величины;
    # некогерентная часть усредняется в ноль
    cross_mag = float(np.abs(cross_mean))
    model_mag = float(np.mean(np.abs(S_LR_ph[mb])))
    out["known_phase_check"] = {
        "model_phase_rad": ph_model, "measured_phase_rad": round(ph_meas, 4),
        "phase_match": bool(abs(ph_meas - ph_model) < 0.05),
        "model_cross_psd": round(model_mag, 6),
        "measured_cross_psd": round(cross_mag, 6),
        "amplitude_ratio": round(cross_mag / max(model_mag, 1e-30), 3),
        "amplitude_match": bool(abs(cross_mag /
                                    max(model_mag, 1e-30) - 1.0) < 0.1)}
    print(f"    known-phase: модель +{ph_model} рад → измерено "
          f"{ph_meas:+.4f} рад; |cross| модель {model_mag:.3e} → "
          f"измерено {cross_mag:.3e}", flush=True)
    for lo, hi in ((900, 1100), (100, 500), (500, 2000), (2000, 8000),
                   (8000, 24000)):
        mb = (bins >= lo) & (bins <= hi)
        g2_syn = float(np.mean(np.abs(aLR[mb]) ** 2 /
                               np.maximum(aLL[mb] * aRR[mb], 1e-30)))
        dL = float(np.mean(10 * np.log10(aLL[mb] / np.maximum(S_LL[mb], 1e-30))))
        dR = float(np.mean(10 * np.log10(aRR[mb] / np.maximum(S_RR[mb], 1e-30))))
        ph_syn = float(np.angle(np.mean(aLR[mb])))
        ph_mod = float(np.angle(np.mean(S_LR[mb])))
        out["bands"].append({"band_hz": [lo, hi],
                             "gamma2_synthesized": round(g2_syn, 3),
                             "psd_L_dev_db": round(dL, 2),
                             "psd_R_dev_db": round(dR, 2),
                             "cross_phase_syn_rad": round(ph_syn, 3),
                             "cross_phase_model_rad": round(ph_mod, 3)})
        print(f"    [{lo:>5}-{hi:<5}] Гц: γ² {g2_syn:.3f} | PSD L {dL:+.2f} "
              f"R {dR:+.2f} дБ | фаза {ph_syn:+.3f}/{ph_mod:+.3f} рад",
              flush=True)
    out["gamma2_model_mean"] = None
    # полные target/generated спектры → npz (§21 п.6)
    g2_model_full = np.abs(S_LR) ** 2 / np.maximum(S_LL * S_RR, 1e-30)
    out["gamma2_model_mean"] = round(float(np.mean(g2_model_full)), 4)
    npz_path = OUT / "v2187-verify-colored-spectra.npz"
    np.savez_compressed(
        npz_path,
        freqs=bins, target_S_LL=S_LL, target_S_RR=S_RR, target_S_LR=S_LR,
        generated_S_LL=aLL, generated_S_RR=aRR, generated_S_LR=aLR,
        gamma2_model=g2_model_full,
        gamma2_generated=np.abs(aLR) ** 2 /
        np.maximum(aLL * aRR, 1e-30))
    out["spectra_npz"] = npz_path.name
    out["spectra_npz_sha256"] = __import__("hashlib").sha256(
        npz_path.read_bytes()).hexdigest()
    print(f"    спектры target/generated → {npz_path.name}", flush=True)
    return out


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
        print(f"COLORED: {spectra[4]} тихих окон; уплощено бинов: "
              f"{spectra[5]['n_bins_flattened']}", flush=True)
    if args.aggregate:
        summary, _ = aggregate(args.model)
        json.dump(summary, open(OUT / f"v2187-montecarlo-{args.model}-summary.json",
                                "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"JSON: v2187-montecarlo-{args.model}-summary.json", flush=True)
        return
    if args.batch is None:
        if args.model == "colored":
            ver = verify_models(*spectra[:4])
            ver["model_info"] = spectra[5]
            json.dump(ver, open(OUT / "v2187-verify-colored.json", "w",
                                encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print(json.dumps(ver["known_phase_check"], ensure_ascii=False),
                  flush=True)
        return
    run_batch(args.model, args.batch, args.trials_per_batch, cal, spectra)


if __name__ == "__main__":
    main()
