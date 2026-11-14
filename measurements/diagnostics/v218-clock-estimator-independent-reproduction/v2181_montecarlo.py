#!/usr/bin/env python3
"""v2.18.1 п.1+3 — Монте-Карло на НЕЗАВИСИМЫХ реализациях коррелированного
цветного шума, синтезированного по измеренным PSD/cross-PSD L/R.

Метод синтеза (условное разложение, точно per-bin):
  gR(f) = Re(S_LR)/S_LL           — оптимальный линейный коэффициент L→R
  S_iR(f) = S_RR − Re(S_LR)²/S_LL — условная дисперсия R при известном L
  n_L = IFFT(√(S_LL·n)·W_c)       — общая реализация (форма S_LL)
  n_R = IFFT(gR·√(S_LL·n)·W_c + √(S_iR·n)·W_i) — форма S_RR с точной
  кросс-корреляцией S_LR на каждом бине. W_c, W_i — независимые комплексные
  гауссовы спектры. Измеренные спектры интерполируются с сетки Welch
  (23.4 Гц, 129 тихих окон v2.18) на сетку 1 Гц целевого окна.

σ_pred: формула профильной кривизны ПЕРЕВЫВЕДЕНА (п.3 задания):
  logL(f) = −RSS(f)/(2σ²) → FI(f̂) = RSS''(f̂)/(2σ²) → var(f̂) = 2σ²/RSS''
  — в v2.18 множитель 2 был пропущен (σ_pred занижалась в √2: 0.16 против
  фактических 0.23). Здесь var_f = 2·step²/denom, проверка — фактическая σ
  MC и покрытие 1σ/2σ. sigma_pred НЕ передаётся в stats() с вычитанием
  PPM_TRUE — отдельная структура.

500 прогонов на уровень −72/−66/−60 dBFS; per-trial данные (seed, ppm,
ошибки) — полностью в CSV. Playback НЕТ; production только импортируется.
Фит LSQ — на 48 кГц (однократная децимация, est_lsq v2.18.1 с независимым
стартом; двойная децимация v2.18 устранена).
"""
import argparse
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
from v218_estimators_real import est_lsq, est_joint, ppm_of  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 192000
TRIALS = 500
LEVELS = [-72.0, -66.0, -60.0]
PPM_TRUE = -23.5
BASE_SEED = 218100
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


def quiet_windows(cap):
    """Тихие окна 0.25 с (критерий v2.18: RMS < P25+3 дБ, участки ≥ 0.35 с)."""
    w = int(0.05 * FS)
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
                    s = int((i * 0.05 + k * 0.25) * FS)
                    out.append((s, s + int(0.25 * FS)))
            i = j
        else:
            i += 1
    return out


def measured_spectra(source_dir):
    """Ансамбль: усреднённые S_LL, S_RR, S_LR тихих окон всех 7 захватов."""
    nper = 8192
    S_LL = S_RR = S_LR = None
    n_win = 0
    for idx in IDX:
        _, cap = wavfile.read(str(Path(source_dir) / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        for (a, b) in quiet_windows(cap):
            f, P_LL = welch(cap[a:b, 0], FS, nperseg=nper)
            _, P_RR = welch(cap[a:b, 1], FS, nperseg=nper)
            _, P_LR = csd(cap[a:b, 0], cap[a:b, 1], FS, nperseg=nper)
            S_LL = P_LL if S_LL is None else S_LL + P_LL
            S_RR = P_RR if S_RR is None else S_RR + P_RR
            S_LR = P_LR if S_LR is None else S_LR + P_LR
            n_win += 1
    return f, S_LL / n_win, S_RR / n_win, S_LR / n_win, n_win


def synth_pair_from_spectra(f_m, S_LL_m, S_RR_m, S_LR_m, rng, fs=48000.0):
    """Пара шума 2 с @fs с per-bin точными S_LL/S_RR/S_LR
    (условное разложение по L; сетка интерполяции = ЧАСТОТЫ бинов fs/n).
    Возвращает (n_L, n_R)."""
    n = int(2.0 * fs)
    df_t = FS / n                                     # 0.5 Гц (n = 384000!)
    # ЧАСТОТЫ бинов, не индексы: баг черновика v2.18.1 — interp по индексам
    # сжимал спектр вдвое по частоте
    f_t = np.arange(n // 2 + 1).astype(np.float64) * df_t
    S_LL = np.interp(f_t, f_m, S_LL_m)
    S_RR = np.interp(f_t, f_m, S_RR_m)
    ReLR = np.interp(f_t, f_m, np.real(S_LR_m))
    ImLR = np.interp(f_t, f_m, np.imag(S_LR_m))
    S_LR = ReLR + 1j * ImLR
    # сглаживание оценок спектров (v2.18.1): per-bin оценка γ² имеет
    # дисперсию — на слабокогерентных ВЧ-бинах γ²_est > 1, кламп привёл бы
    # к фиктивной когерентности 1.0. Скользящее среднее ±10 Гц сохраняет
    # структуру (мейнс-линии, спад) и убирает дисперсию оценки.
    kernel = np.ones(41) / 41.0                        # ±10 Гц на сетке 0.5 Гц
    S_LL = np.convolve(S_LL, kernel, mode="same")
    S_RR = np.convolve(S_RR, kernel, mode="same")
    S_LR = np.convolve(S_LR, kernel, mode="same")
    # условное разложение с КОМПЛЕКСНОЙ регрессией (v2.18.1: Re-вариант
    # терял мнимую часть кросс-спектра → γ² занижалась)
    g2_model = np.abs(S_LR) ** 2 / np.maximum(S_LL * S_RR, 1e-30)
    g2_model = np.clip(g2_model, 0.0, 0.995)
    amp_cross = np.sqrt(g2_model * S_LL * S_RR)        # |S_LR| по модели
    phase_cross = np.exp(1j * np.angle(S_LR))
    S_LR_model = amp_cross * phase_cross
    gR_c = S_LR_model / np.maximum(S_LL, 1e-30)        # комплексный L→R
    S_iR = S_RR * (1.0 - g2_model)                     # условная дисперсия R
    def gauss_half():
        h = (rng.standard_normal(len(f_t)) +
             1j * rng.standard_normal(len(f_t))) / math.sqrt(2)
        h[0] = rng.standard_normal()
        h[-1] = rng.standard_normal()                 # Найквист — вещественный
        return h
    Wc = gauss_half()
    Wi = gauss_half()
    # масштаб: E|H[k]|² = S(f_k)·fs·n/2 → var(irfft) = ∫S df (точный)
    C_spec = np.sqrt(S_LL * FS * n / 2.0) * Wc
    I_spec = np.sqrt(S_iR * FS * n / 2.0) * Wi
    n_L = np.fft.irfft(C_spec, n)
    n_R = np.fft.irfft(gR_c * C_spec + I_spec, n)
    return n_L, n_R


def normalize_pair_to_band(nL, nR, f_m, S_LL_m, S_RR_m, band=(900.0, 1100.0)):
    """Приведение фактической дисперсии пары к модели в полосе тона
    (модель: интеграл измеренной односторонней PSD по полосе)."""
    m = (f_m >= band[0]) & (f_m <= band[1])
    tL = float(np.trapz(S_LL_m[m], f_m[m]))
    tR = float(np.trapz(S_RR_m[m], f_m[m]))
    return nL * math.sqrt(tL / max(np.var(nL), 1e-30)), \
        nR * math.sqrt(tR / max(np.var(nR), 1e-30))


def verify_synthesis(f_m, S_LL_m, S_RR_m, S_LR_m, n_check=40):
    """Проверка модели ПРЯМЫМ методом: per-bin накопление rfft полных 2-с
    реализаций, когерентность — per-bin среднее (та же метрика, что у
    измеренного ансамбля v2.18). welch на 1-с срезе не используется:
    утечка НЧ-дрейфа модели в ВЧ-полосы; агрегированная (когерентная сумма)
    когерентность занижает γ² из-за вращения фаз кросс-спектра по бинам."""
    rng = np.random.default_rng(BASE_SEED + 777)
    fs_syn = 48000.0                                  # синтез в MC на 48 кГц
    n = int(2.0 * fs_syn)
    bins = np.arange(n // 2 + 1).astype(np.float64)
    df_bins = fs_syn / n
    bands = [(900, 1100), (100, 500), (500, 2000), (2000, 8000),
             (8000, 30000), (30000, 80000)]
    aLL = np.zeros(n // 2 + 1, dtype=complex)
    aRR = np.zeros(n // 2 + 1, dtype=complex)
    aLR = np.zeros(n // 2 + 1, dtype=complex)
    for _ in range(n_check):
        nL, nR = synth_pair_from_spectra(f_m, S_LL_m, S_RR_m, S_LR_m, rng)
        FL = np.fft.rfft(nL)
        FR = np.fft.rfft(nR)
        aLL += np.abs(FL) ** 2
        aRR += np.abs(FR) ** 2
        aLR += FL * np.conj(FR)
    out = []
    for lo, hi in bands:
        mm = (f_m >= lo) & (f_m <= hi)
        mb = (bins >= lo) & (bins <= hi)
        S_LL_syn = (2 / n ** 2) * aLL[mb] / n_check
        S_RR_syn = (2 / n ** 2) * aRR[mb] / n_check
        S_LR_syn = (2 / n ** 2) * aLR[mb] / n_check
        S_LL_t = np.interp(bins[mb], f_m, S_LL_m)
        g2_meas = float(np.mean(np.abs(S_LR_m[mm]) ** 2 /
                                (S_LL_m[mm] * S_RR_m[mm])))
        g2_syn = float(np.mean(np.abs(S_LR_syn) ** 2 / np.maximum(
            S_LL_syn * S_RR_syn, 1e-30)))
        dL = float(np.mean(10 * np.log10(S_LL_syn / S_LL_t)))
        out.append({"band_hz": [lo, hi],
                    "gamma2_measured": round(g2_meas, 3),
                    "gamma2_synthesized": round(g2_syn, 3),
                    "psd_shape_dev_db_L": round(dL, 2)})
        print(f"    [{lo:>5}-{hi:<5}] Гц: γ² изм {g2_meas:.3f} / син {g2_syn:.3f} | "
              f"PSD L син/мод {dL:+.2f} дБ", flush=True)
    return {"bands": out, "check_trials": n_check,
            "method": ("прямой per-bin rfft; когерентность — per-bin среднее "
                       "(метрика измеренного ансамбля)")}


def white_corr_pair(n, seed):
    """Белый коррелированный эквивалент (v2.18, верифицирован): общий
    источник + независимые добавки; полы L −80 / R −96 дБFS, ρ ≈ 0.97."""
    rng = np.random.default_rng(seed)
    g = 10 ** (-16.0 / 20.0)
    vL = dsp.dbfs_to_amp(NOISE_L_DBFS) ** 2
    vR = dsp.dbfs_to_amp(NOISE_R_DBFS) ** 2
    sc2 = 0.97 * math.sqrt(vL * vR) / g
    siL2 = max(vL - sc2, 1e-30)
    siR2 = max(vR - sc2 * g * g, 1e-30)
    common = math.sqrt(sc2) * rng.standard_normal(n)
    nL = common + math.sqrt(siL2) * rng.standard_normal(n)
    nR = g * common + math.sqrt(siR2) * rng.standard_normal(n)
    return nL, nR


def run_batch(batch_idx, trials_per_batch, cal, f_m, S_LL_m, S_RR_m, S_LR_m):
    """Один батч: trials_per_batch прогонов на уровень, аппенд в CSV.
    OOM-устойчивость: каждый батч — отдельный процесс (~60 МБ)."""
    FS_M = 48000.0
    N_M = int(2.0 * FS_M)
    csv_path = OUT / f"v2181-trials-batch-{batch_idx:02d}.csv"
    rows = []
    for li, level in enumerate(LEVELS):
        for tr in range(trials_per_batch):
            seed = BASE_SEED + li * 100000 + batch_idx * trials_per_batch + tr
            rng = np.random.default_rng(seed)
            nL, nR = synth_pair_from_spectra(f_m, S_LL_m, S_RR_m, S_LR_m, rng,
                                             fs=FS_M)
            nL, nR = normalize_pair_to_band(nL, nR, f_m, S_LL_m, S_RR_m)
            phase0 = 2 * math.pi * ((seed * 37) % 997) / 997
            t = np.arange(N_M) / FS_M
            tone = dsp.dbfs_to_amp(level) * np.sin(
                2 * np.pi * 1000.0 * (1 + PPM_TRUE * 1e-6) * t + phase0)
            L = tone + nL[:N_M]
            R = tone + nR[:N_M]
            row = {"seed": seed, "level_dbfs": level}
            rLp = dsp.analyze_tone(L, FS_M, 1000.0, N_M, nharm=2,
                                   band=(20.0, cal["band_hi"]), mains=cal["mains"])
            rRp = dsp.analyze_tone(R, FS_M, 1000.0, N_M, nharm=2,
                                   band=(20.0, cal["band_hi"]), mains=cal["mains"])
            row["ppm_prod_L"] = (rLp["f0"] / 1000.0 - 1) * 1e6
            row["ppm_prod_R"] = (rRp["f0"] / 1000.0 - 1) * 1e6
            row["ppm_prod_mean"] = (row["ppm_prod_L"] + row["ppm_prod_R"]) / 2
            row["ppm_lsq_L"] = ppm_of(est_lsq(L, FS_M, 1000.0, N_M))
            row["ppm_lsq_R"] = ppm_of(est_lsq(R, FS_M, 1000.0, N_M))
            jg = est_joint(np.column_stack([L, R]), FS_M, 1000.0, "gls")
            row["ppm_joint_gls"] = ppm_of(jg["f"])
            row["sigma_pred_profile_ppm"] = (
                math.sqrt(2.0 * jg["var_f"]) / 1000.0 * 1e6
                if jg["var_f"] else None)
            row["err_prod_L"] = row["ppm_prod_L"] - PPM_TRUE
            row["err_prod_R"] = row["ppm_prod_R"] - PPM_TRUE
            row["err_prod_mean"] = row["ppm_prod_mean"] - PPM_TRUE
            row["err_lsq_L"] = row["ppm_lsq_L"] - PPM_TRUE
            row["err_lsq_R"] = row["ppm_lsq_R"] - PPM_TRUE
            row["err_joint_gls"] = row["ppm_joint_gls"] - PPM_TRUE
            rows.append(row)
            del L, R, nL, nR, tone
    keys = list(rows[0].keys())
    with open(csv_path, "w") as fh:
        fh.write(",".join(keys) + "\n")
        for row in rows:
            fh.write(",".join(str(row[k]) for k in keys) + "\n")
    print(f"батч {batch_idx}: {len(rows)} строк → {csv_path.name}", flush=True)


def aggregate():
    """Агрегация всех батчей: per-trial CSV merged + сводка + sigma_pred stats."""
    import csv as csv_mod
    rows = []
    for p in sorted(OUT.glob("v2181-trials-batch-*.csv")):
        with open(p) as fh:
            for r in csv_mod.DictReader(fh):
                rows.append({k: float(v) if k != "seed" else int(v)
                             for k, v in r.items()})
    summary = []
    for level in LEVELS:
        sel = [r for r in rows if r["level_dbfs"] == level]
        rec = {"level_dbfs": level, "trials": len(sel), "estimators": {}}
        for k in ("prod_L", "prod_R", "lsq_L", "lsq_R", "joint_gls"):
            a = np.asarray([r["err_" + k] for r in sel])
            rec["estimators"][k] = {
                "bias_ppm": round(float(np.mean(a)), 3),
                "sigma_ppm": round(float(np.std(a)), 3),
                "max_abs_ppm": round(float(np.max(np.abs(a))), 3)}
        spv = np.asarray([r["sigma_pred_profile_ppm"] for r in sel])
        jg_err = np.asarray([r["err_joint_gls"] for r in sel])
        mean_sp = float(np.mean(spv))
        rec["sigma_pred_stats"] = {
            "mean_pred_ppm": round(mean_sp, 3),
            "median_pred_ppm": round(float(np.median(spv)), 3),
            "actual_sigma_ppm": rec["estimators"]["joint_gls"]["sigma_ppm"],
            "pred_over_actual": round(mean_sp / max(
                rec["estimators"]["joint_gls"]["sigma_ppm"], 1e-9), 3),
            "coverage_1sigma": round(float(np.mean(np.abs(jg_err) <= mean_sp)), 3),
            "coverage_2sigma": round(float(np.mean(np.abs(jg_err) <=
                                                   2 * mean_sp)), 3),
            "derivation": ("logL = −RSS/(2σ²) → FI = RSS''/(2σ²) → "
                           "var(f̂) = 2σ²/RSS''; whitened σ=1 → var = 2·step²/denom; "
                           "в v2.18 множитель 2 был пропущен")}
        summary.append(rec)
        st = rec["sigma_pred_stats"]
        print(f"{level:+6.0f} dBFS (n={len(sel)}): prod L σ={rec['estimators']['prod_L']['sigma_ppm']:.2f} "
              f"R σ={rec['estimators']['prod_R']['sigma_ppm']:.2f} | "
              f"lsq L σ={rec['estimators']['lsq_L']['sigma_ppm']:.2f} "
              f"R σ={rec['estimators']['lsq_R']['sigma_ppm']:.2f} | "
              f"jGLS σ={rec['estimators']['joint_gls']['sigma_ppm']:.2f} | "
              f"σ_pred {st['mean_pred_ppm']:.2f} "
              f"(pred/actual {st['pred_over_actual']}) | "
              f"cover 1σ {st['coverage_1sigma']:.2f} 2σ {st['coverage_2sigma']:.2f}",
              flush=True)
    with open(OUT / "v2181-montecarlo-trials.csv", "w") as fh:
        keys = list(rows[0].keys())
        fh.write(",".join(keys) + "\n")
        for row in rows:
            fh.write(",".join(str(row[k]) for k in keys) + "\n")
    json.dump({"noise_model": {"windows": 129,
                               "source": "7 захватов v2.14, тихие окна 0.25 с",
                               "synthesis_method": ("условное разложение по L: "
                               "gR = S_LR/S_LL (комплексный), S_iR = S_RR−|S_LR|²/S_LL; "
                               "сглаживание γ² ±10 Гц (дисперсия per-bin оценки)"),
                               "synthesis_verification": LAST_VER},
               "trials_per_cell": len(rows) // 3, "base_seed": BASE_SEED,
               "ppm_true": PPM_TRUE, "summary": summary},
              open(OUT / "v2181-montecarlo-summary.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("per-trial CSV: {len(rows)} строк; "
          f"JSON: v2181-montecarlo-summary.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="/opt/MyApps/Adudio/"
                    "ValveBench-v214-clock-diagnostic/measurements/"
                    "diagnostics/v214-clock-diagnostic")
    ap.add_argument("--batch", type=int, default=None,
                    help="индекс батча (0..19); None = верификация+агрегация")
    ap.add_argument("--trials-per-batch", type=int, default=25)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    global LAST_VER
    cal = calib.Calibration()
    f_m, S_LL_m, S_RR_m, S_LR_m, n_win = measured_spectra(args.source_dir)
    if args.batch is None:
        print(f"модель шума: {n_win} тихих окон из 7 захватов v2.14", flush=True)
        LAST_VER = verify_synthesis(f_m, S_LL_m, S_RR_m, S_LR_m)
        print(f"верификация синтеза: {len(LAST_VER['bands'])} полос", flush=True)
        aggregate()
        return
    run_batch(args.batch, args.trials_per_batch, cal, f_m, S_LL_m, S_RR_m, S_LR_m)
if __name__ == "__main__":
    main()
