#!/usr/bin/env python3
"""v2.18.3 — Монте-Карло clock estimator: НОВЫЙ оценщик (независимый
LSQ с ограниченной параболой, v2183_estimators) + fit-rate в результатах.

Модель WHITE (основной эксперимент, описана в отчёте v2.18.1):
  белый коррелированный эквивалент — общий источник (уровень = пол L),
  в R с передачей −16 дБ, независимые добавки; полы L −80 / R −96 дБFS
  (= DURING-TONE состояние тракта по метрике production noise_dbfs);
  ρ ≈ 0.97. Все бины одинаково коррелированы (нулевой межканальный
  временной сдвиг) — ограничение модели, документировано.

Модель COLORED (отдельный эксперимент): синтез по измеренным
  PSD/cross-PSD тихих окон (ансамбль 129 окон v2.18). Ограничение:
  тихие окна = D10 молчит → модель = пол E-MU БЕЗ шума D10 во время
  воспроизведения; для during-tone уровня требуется масштабирование,
  которое в v2.18.1 не было доказано (замечание ревизии) — здесь
  модель используется КАК ИЗМЕРЕНА (пол E-MU), без 12×-интерпретации.

Исправления по ревизии v2.18.1:
  * частотная сетка синтеза: df_t = fs/n (0.5 Гц при 48 кГц, n = 96000) —
    не FS/n (глобальный 192000);
  * масштаб Фурье-коэффициентов: |H[k]|² = S(f_k)·fs·n/2 (fs — фактическая);
  * нормализация: полосная дисперсия в 900–1100 Гц через rfft — приводится
    к интегралу измеренной PSD в ТОЙ ЖЕ полосе (не к полной дисперсии);
  * верификация: PSD per-bin = |F|²·2/(fs·n) (с нормировкой на частоту
    дискретизации); полосы только ≤ Найквист синтеза;
  * sigma_pred: per-trial покрытие |err_i| ≤ σ_pred_i (не средняя σ);
    var(f̂) = 2σ²/RSS'' (множитель 2 из вывода logL = −RSS/2σ²).

Playback НЕТ; production только импортируется. Фит всех оценщиков — 48 кГц
(однократная децимация, задокументировано).
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
from scipy.signal import csd, welch, resample_poly

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from valvebench import calib, dsp  # noqa: E402
from v2183_estimators import est_lsq, est_joint, ppm_of_pilot  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS_MC = 48000.0                    # частота MC (все оценщики и синтез)
N_MC = int(2.0 * FS_MC)            # 96000
TRIALS_PER_BATCH = 25
LEVELS = [-72.0, -66.0, -60.0]
PPM_TRUE = -23.5
BASE_SEED = 218300
NOISE_L_DBFS = -80.0
NOISE_R_DBFS = -96.0
RHO_TARGET = 0.97
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


# ------------------------------------------------------------ модели шума

def noise_white_corr(n, seed):
    """WHITE: общий источник (пол L) + передача в R −16 дБ + независимые
    добавки. Все бины одинаково коррелированы (нулевой межканальный
    временной сдвиг) — ограничение модели, документировано в отчёте."""
    rng = np.random.default_rng(seed)
    g = 10 ** (-16.0 / 20.0)
    vL = dsp.dbfs_to_amp(NOISE_L_DBFS) ** 2
    vR = dsp.dbfs_to_amp(NOISE_R_DBFS) ** 2
    sc2 = RHO_TARGET * math.sqrt(vL * vR) / g
    siL2 = max(vL - sc2, 1e-30)
    siR2 = max(vR - sc2 * g * g, 1e-30)
    common = math.sqrt(sc2) * rng.standard_normal(n)
    nL = common + math.sqrt(siL2) * rng.standard_normal(n)
    nR = g * common + math.sqrt(siR2) * rng.standard_normal(n)
    return nL, nR


FS_CAPTURE = 192000                # исходные захваты v2.14 — 192 кГц


def quiet_windows(cap):
    w = int(0.05 * FS_CAPTURE)
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
                    s = int((i * 0.05 + k * 0.25) * FS_CAPTURE)
                    out.append((s, s + int(0.25 * FS_CAPTURE)))
            i = j
        else:
            i += 1
    return out


def measured_spectra(source_dir, fs_syn):
    """Ансамбль тихих окон: усреднённые S_LL/S_RR/S_LR, интерполированные
    на частотную сетку синтеза (fs_syn/n = 0.5 Гц)."""
    nper = 8192
    S_LL = S_RR = S_LR = None
    n_win = 0
    for idx in IDX:
        _, cap = wavfile.read(str(Path(source_dir) / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        for (a, b) in quiet_windows(cap):
            f, P_LL = welch(cap[a:b, 0], FS_CAPTURE, nperseg=nper)
            _, P_RR = welch(cap[a:b, 1], FS_CAPTURE, nperseg=nper)
            _, P_LR = csd(cap[a:b, 0], cap[a:b, 1], FS_CAPTURE, nperseg=nper)
            S_LL = P_LL if S_LL is None else S_LL + P_LL
            S_RR = P_RR if S_RR is None else S_RR + P_RR
            S_LR = P_LR if S_LR is None else S_LR + P_LR
            n_win += 1
    S_LL /= n_win; S_RR /= n_win; S_LR /= n_win
    n = int(2.0 * fs_syn)
    f_t = np.arange(n // 2 + 1).astype(np.float64) * (fs_syn / n)   # Гц!
    S_LL_t = np.interp(f_t, f, S_LL)
    S_RR_t = np.interp(f_t, f, S_RR)
    ReLR = np.interp(f_t, f, np.real(S_LR))
    ImLR = np.interp(f_t, f, np.imag(S_LR))
    return f_t, S_LL_t, S_RR_t, ReLR + 1j * ImLR, n_win


def noise_colored(n, seed, S_LL, S_RR, S_LR):
    """COLORED: условное разложение по L на сетке fs/n.
    gR_c = S_LR/S_LL (комплексный); S_iR = S_RR − |S_LR|²/S_LL;
    |H[k]|² = S(f_k)·fs·n/2 → var(irfft) = ∫S df (fs/n = 0.5 Гц ✓).
    Сглаживание оценок ±10 Гц (дисперсия per-bin γ²-оценки)."""
    rng = np.random.default_rng(seed)
    kernel = np.ones(41) / 41.0
    S_LL_s = np.convolve(S_LL, kernel, mode="same")
    S_RR_s = np.convolve(S_RR, kernel, mode="same")
    S_LR_s = np.convolve(S_LR, kernel, mode="same")
    g2_model = np.clip(np.abs(S_LR_s) ** 2 /
                       np.maximum(S_LL_s * S_RR_s, 1e-30), 0.0, 0.995)
    S_LR_model = np.sqrt(g2_model * S_LL_s * S_RR_s) * \
        np.exp(1j * np.angle(S_LR_s))
    gR_c = S_LR_model / np.maximum(S_LL_s, 1e-30)
    S_iR = S_RR_s * (1.0 - g2_model)

    def gauss_half():
        h = (rng.standard_normal(len(S_LL)) +
             1j * rng.standard_normal(len(S_LL))) / math.sqrt(2)
        h[0] = rng.standard_normal()
        h[-1] = rng.standard_normal()
        return h
    Wc = gauss_half()
    Wi = gauss_half()
    fs_c = 48000.0
    C_spec = np.sqrt(S_LL_s * fs_c * n / 2.0) * Wc
    I_spec = np.sqrt(S_iR * fs_c * n / 2.0) * Wi
    n_L = np.fft.irfft(C_spec, n)
    n_R = np.fft.irfft(gR_c * C_spec + I_spec, n)
    return n_L, n_R


# --------------------------------------------------- верификация моделей

def verify_white(n_trials=40, seed=BASE_SEED + 900):
    """WHITE: теоретическая ρ = 0.97; измерение фактической ρ по MC-парам."""
    errs = []
    half = int(FS_MC // 2)          # 0.5 с — количество сэмплов, целое
    for i in range(n_trials):
        nL, nR = noise_white_corr(int(2.0 * FS_MC), seed + i)
        errs.append(float(np.corrcoef(nL[:half], nR[:half])[0, 1]))
    return {"model": "white_corr", "rho_theory": RHO_TARGET,
            "rho_measured_mean": round(float(np.mean(errs)), 3),
            "rho_measured_std": round(float(np.std(errs)), 3),
            "trials": n_trials,
            "floors_dbfs": [NOISE_L_DBFS, NOISE_R_DBFS],
            "limitation": "нулевой межканальный временной сдвиг во всех бинах"}


def verify_colored(f_t, S_LL, S_RR, S_LR, n_check=15):
    """COLORED: per-bin PSD = |F|²·2/(fs·n) (с нормировкой fs), когерентность
    per-bin среднее; полосы ≤ Найквист синтеза (24 кГц при 48 кГц)."""
    rng = np.random.default_rng(BASE_SEED + 901)
    n = int(2.0 * FS_MC)
    bins = np.arange(n // 2 + 1).astype(np.float64) * (FS_MC / n)  # ЧАСТОТЫ!
    aLL = np.zeros(len(bins))
    aRR = np.zeros(len(bins))
    aLR = np.zeros(len(bins), dtype=complex)
    for i in range(n_check):
        nL, nR = noise_colored(n, BASE_SEED + 902 + i, S_LL, S_RR, S_LR)
        FL = np.fft.rfft(nL)
        FR = np.fft.rfft(nR)
        # накапливаем СРЕДНЕЕ по реализациям (ревизия v2.18.2: без деления
        # на n_check PSD завышалась на 10·log10(n_check) ≈ 11.8 дБ)
        aLL += np.abs(FL) ** 2 * 2.0 / (FS_MC * n) / n_check
        aRR += np.abs(FR) ** 2 * 2.0 / (FS_MC * n) / n_check
        aLR += FL * np.conj(FR) * 2.0 / (FS_MC * n) / n_check
    out = []
    for lo, hi in ((900, 1100), (100, 500), (500, 2000), (2000, 8000),
                   (8000, 24000)):
        mb = (bins >= lo) & (bins <= hi)
        g2_syn = float(np.mean(np.abs(aLR[mb]) ** 2 /
                               np.maximum(aLL[mb] * aRR[mb], 1e-30)))
        dL = float(np.mean(10 * np.log10(aLL[mb] / np.maximum(S_LL[mb], 1e-30))))
        dR = float(np.mean(10 * np.log10(aRR[mb] / np.maximum(S_RR[mb], 1e-30))))
        out.append({"band_hz": [lo, hi],
                    "gamma2_synthesized": round(g2_syn, 3),
                    "psd_L_dev_db": round(dL, 2),
                    "psd_R_dev_db": round(dR, 2)})
        print(f"    [{lo:>5}-{hi:<5}] Гц: γ² синт {g2_syn:.3f} | "
              f"PSD L {dL:+.2f} дБ, R {dR:+.2f} дБ", flush=True)
    return {"bands": out, "check_trials": n_check,
            "nyquist_note": f"полосы > {FS_MC/2:.0f} кГц невалидны для 48к-синтеза"}


# --------------------------------------------------------- оценщики/MC

def run_batch(model, batch_idx, trials_per_batch, cal, spectra):
    FS_M = FS_MC
    N_M = N_MC
    csv_path = OUT / f"v2183-trials-{model}-batch-{batch_idx:02d}.csv"
    rows = []
    script_hash = hashlib.sha256(open(Path(__file__).resolve(), "rb").read()).hexdigest()
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
            t = np.arange(N_M) / FS_M
            f_true = 1000.0 * (1 + PPM_TRUE * 1e-6)
            tone = dsp.dbfs_to_amp(level) * np.sin(2 * np.pi * f_true * t + phase0)
            L = tone + nL
            R = tone + nR
            row = {"seed": seed, "level_dbfs": level, "model": model}
            rLp = dsp.analyze_tone(L, FS_M, 1000.0, N_M, nharm=2,
                                   band=(20.0, cal["band_hi"]), mains=cal["mains"])
            rRp = dsp.analyze_tone(R, FS_M, 1000.0, N_M, nharm=2,
                                   band=(20.0, cal["band_hi"]), mains=cal["mains"])
            row["ppm_prod_L"] = (rLp["f0"] / 1000.0 - 1) * 1e6
            row["ppm_prod_R"] = (rRp["f0"] / 1000.0 - 1) * 1e6
            row["fit_rate_prod"] = FS_M
            row["ppm_prod_mean"] = (row["ppm_prod_L"] + row["ppm_prod_R"]) / 2
            f_lsL, rate_L = est_lsq(L, FS_M, 1000.0)
            f_lsR, rate_R = est_lsq(R, FS_M, 1000.0)
            row["fit_rate_lsq_L"] = rate_L
            row["fit_rate_lsq_R"] = rate_R
            row["ppm_lsq_L"] = ppm_of_pilot(f_lsL, 1000.0)
            row["ppm_lsq_R"] = ppm_of_pilot(f_lsR, 1000.0)
            jg = est_joint(np.column_stack([L, R]), FS_M, 1000.0)
            row["fit_rate_joint"] = jg[2]
            row["ppm_joint_gls"] = ppm_of_pilot(jg[0], 1000.0)
            # σ_pred: var = 2σ²/RSS'' (множитель 2, вывод в шапке)
            row["sigma_pred_joint_ppm"] = (
                math.sqrt(2.0 * jg[1]) / 1000.0 * 1e6
                if jg[1] else None)
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
    print(f"батч {model}/{batch_idx}: {len(rows)} строк; script sha256 "
          f"{script_hash[:16]}…", flush=True)


def aggregate(model):
    import csv as csv_mod
    rows = []
    for p in sorted(OUT.glob(f"v2183-trials-{model}-batch-*.csv")):
        with open(p) as fh:
            for r in csv_mod.DictReader(fh):
                out_r = {}
                for k, v in r.items():
                    if k == "seed":
                        out_r[k] = int(v)
                    elif k == "model":
                        out_r[k] = v
                    else:
                        try:
                            out_r[k] = float(v)
                        except (ValueError, TypeError):
                            out_r[k] = float("nan") if v == "None" else v
                rows.append(out_r)
    summary = {"model": model, "levels": []}
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
        # sigma_pred: per-trial покрытие (не средняя σ!)
        sp = np.asarray([r["sigma_pred_joint_ppm"] for r in sel
                         if np.isfinite(r.get("sigma_pred_joint_ppm",
                                              float("nan")))])
        jg_err = np.asarray([r["err_joint_gls"] for r in sel
                             if np.isfinite(r.get("sigma_pred_joint_ppm",
                                                  float("nan")))])
        valid = np.isfinite(sp) & np.isfinite(jg_err)
        spv, je = sp[valid], jg_err[valid]
        rec["sigma_pred_stats"] = {
            "n_valid": int(valid.sum()),
            "mean_pred_ppm": round(float(np.mean(spv)), 3),
            "median_pred_ppm": round(float(np.median(spv)), 3),
            "actual_sigma_ppm": round(float(np.std(je)), 3),
            "pred_over_actual": round(float(np.mean(spv)) /
                                      max(float(np.std(je)), 1e-9), 3),
            "coverage_1sigma_per_trial": round(float(np.mean(
                np.abs(je) <= spv)), 3),
            "coverage_2sigma_per_trial": round(float(np.mean(
                np.abs(je) <= 2 * spv)), 3),
            "note": ("var(f̂) = 2σ²/RSS'' (множитель 2, вывод logL = −RSS/2σ²); "
                     "покрытие per-trial; условные результаты для ПРОВЕРЕННОЙ "
                     "модели шума, не для реального аппаратного шума — "
                     "текущий GLS учитывает только нулевой межканальный "
                     "временной сдвиг")}
        summary["levels"].append(rec)
        st = rec["sigma_pred_stats"]
        e = rec["estimators"]
        print(f"{model:8s} {level:+6.0f} (n={len(sel)}): "
              f"prod L σ={e['prod_L']['sigma_ppm']:.2f} R σ={e['prod_R']['sigma_ppm']:.2f} | "
              f"lsq L σ={e['lsq_L']['sigma_ppm']:.2f} R σ={e['lsq_R']['sigma_ppm']:.2f} | "
              f"jGLS σ={e['joint_gls']['sigma_ppm']:.2f} | "
              f"σ_pred mean {st['mean_pred_ppm']:.2f} "
              f"(pred/actual {st['pred_over_actual']}) | "
              f"cover1σ {st['coverage_1sigma_per_trial']:.2f} "
              f"2σ {st['coverage_2sigma_per_trial']:.2f}", flush=True)
    return summary, rows


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
        print(f"COLORED: модель из {spectra[4]} тихих окон, "
              f"сетка {FS_MC / len(spectra[0]):.2f} Гц", flush=True)
    if args.aggregate:
        summary, rows = aggregate(args.model)
        json.dump(summary, open(OUT / f"v2183-montecarlo-{args.model}-summary.json",
                                "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"JSON: v2183-montecarlo-{args.model}-summary.json; "
              f"per-trial: v2183-trials-{args.model}-*.csv merged")
        return
    if args.batch is None:
        # верификация модели
        if args.model == "white":
            ver = verify_white()
        else:
            ver = verify_colored(spectra[0], spectra[1], spectra[2], spectra[3])
        json.dump(ver, open(OUT / f"v2182-verify-{args.model}.json", "w",
                            encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps(ver, ensure_ascii=False)[:400])
        return
    run_batch(args.model, args.batch, args.trials_per_batch, cal, spectra)


if __name__ == "__main__":
    main()
