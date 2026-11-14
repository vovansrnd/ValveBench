#!/usr/bin/env python3
"""v2.18 пп.1-2 — независимая проверка оценщиков частоты на РЕАЛЬНЫХ захватах.

Оценщики:
  prod_centroid  — production dsp.analyze_tone (спектральный центроид, 7 бинов);
  lsq            — нелинейный МНК-фит синусоиды (амплитуда/фаза/частота свободны),
                   двухстадийный: демод-фазовый наклон как старт + локальный
                  поиск минимума RSS (децимация в 48 кГц для скорости);
  phase_slope    — демодуляция на номинале + LSQ наклона развёрнутой фазы;
  joint_naive    — совместная оценка L/R с ОБЩЕЙ частотой, вес 1/σ²
                   (без учёта корреляции);
  joint_gls      — совместная оценка с ПОЛНОЙ ковариационной матрицей ошибок
                   L/R (GLS, ковариация оценивается по остаткам итеративно).

Каждый оценщик применяется к одним и тем же окнам реальных захватов v2.14:
  * production-окно (R-onset + settle 0.4, 2.0 с) — как в v2.16/v2.17;
  * fixed-окно [2.255, 4.255] — как в v2.16 сравнение.
Плюс воспроизведение production-оценок и проверка уровней PSD/шума.

Playback НЕТ; production-код только импортируется, не изменяется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from valvebench import calib, dsp  # noqa: E402

SRC = Path("/opt/MyApps/Adudio/ValveBench-v214-clock-diagnostic/"
           "measurements/diagnostics/v214-clock-diagnostic")
OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
N = 192000                     # 1-с блок production (df = 1 Гц)
IDX = [1, 2, 15, 16, 3, 4, 8]
TRUTH_PPM = -23.5              # консенсус −30 dBFS измерений
CAPTURES = {
    1: "20261114-004425-clock-01--72dB-capture.wav",
    2: "20261114-004425-clock-02--66dB-capture.wav",
    15: "20261114-004425-clock-15--66dB-capture.wav",
    16: "20261114-004425-clock-16--72dB-capture.wav",
    3: "20261114-004425-clock-03--60dB-capture.wav",
    4: "20261114-004425-clock-04--54dB-capture.wav",
    8: "20261114-004425-clock-08--30dB-capture.wav",
}
R_ONSET = {1: 2.25, 2: 1.855, 15: 1.845, 16: 1.855,
           3: 1.85, 4: 1.855, 8: 1.86}          # из v2.16 (R-only onset)


# ------------------------------------------------------------- оценщики

def est_phase_slope(seg, fs, f_nom):
    """Демодуляция на номинале + LSQ наклона развёрнутой фазы.

    ВАЖНО (находка v2.18): когерентный пилот 1000 Гц = ровно fs/192, поэтому
    вещественная демодуляция оставляет зеркало 2·f_nom (2000 Гц) с ТОЙ ЖЕ
    амплитудой — angle(z) скачет на ~π каждые 48 сэмплов и unwrap теряет
    смысл. Зеркало гасится скользящим средним длиной ровно в его период
    (fs/(2·f_nom) = 96 сэмплов); симметричное ядро не вносит фазовой задержки.
    Далее: развёртка фазы, робастный LSQ наклона (единичные скачки фазы
    отбрасываются по MAD разностей), внутренние 90 % окна."""
    n = len(seg)
    t = np.arange(n) / fs
    z = seg * np.exp(-2j * np.pi * f_nom * t)
    per = int(round(fs / (2 * f_nom)))
    if per >= 2:
        z = np.convolve(z, np.ones(per) / per, mode="same")
    ph = np.unwrap(np.angle(z))
    lo, hi = int(0.05 * n), int(0.95 * n)
    d = np.diff(ph[lo:hi])
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med))) or 1e-12
    good = np.where(np.abs(d - med) < max(6 * 1.4826 * mad, 1e-9))[0]
    if len(good) < 100:
        return None, None
    tt = t[lo:hi]
    k1, _ = np.polyfit(tt[good], np.asarray(ph[lo:hi])[good], 1)
    resid = np.asarray(ph[lo:hi])[good] - (k1 * tt[good] + _)
    if float(np.std(resid)) > 1.0:        # фаза недостоверна (тон ниже шума)
        return None, None
    amp = 2.0 * float(np.abs(np.mean(z[lo:hi])))
    return f_nom + k1 / (2 * np.pi), amp


def est_lsq(seg, fs, f_nom, nblock=None, coarse_hz=2.0):
    """Нелинейный МНК-фит синусоиды. v2.18.1 (замечания ревизии): грубая
    оценка НЕЗАВИСИМА от production-центроида — пик оконной периодограммы с
    параболическим уточнением в полосе ±coarse_hz (2 Гц — покрывает весь
    clock-диапазон ±100 ppm = ±0.1 Гц с запасом ~20×); децимация ТОЛЬКО если
    fs > 96 кГц (двойная децимация v2.18 устранена — фактическая частота
    фита: 48 кГц для входа 192 кГц, либо входная fs ≤ 48 кГц как есть);
    поиск МНК ±0.15 Гц вокруг грубой оценки (шаг 0.0125 Гц) + парабола."""
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    # --- независимая грубая оценка: пик периодограммы (Ханн) в ±coarse_hz
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    band = (freqs >= f_nom - coarse_hz) & (freqs <= f_nom + coarse_hz)
    idx_band = np.where(band)[0]
    k = idx_band[int(np.argmax(X[idx_band]))]
    if 0 < k < len(X) - 1:
        y0, y1, y2 = X[k - 1], X[k], X[k + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            k = k + (y0 - y2) / (2 * denom)     # парабола по модулю
    f0 = k * fs / n
    # --- МНК-уточнение ±0.15 Гц (шаг 0.0125 Гц) + парабола
    grid = np.linspace(f0 - 0.15, f0 + 0.15, 25)
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(len(t))])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    f_best = grid[i]
    if 0 < i < len(grid) - 1:      # параболическое уточнение внутри шага
        denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom > 0:
            f_best += (rss[i - 1] - rss[i + 1]) / (2 * denom) * (grid[1] - grid[0])
    return f_best


def est_joint(seg_pair, fs, f_nom, mode):
    """Совместная оценка: общая частота, отдельные амплитуды/фазы L и R.
    mode='naive' — вес 1/σ² (диагональная ковариация);
    mode='gls'   — полная ковариационная матрица ошибок L/R (GLS),
    ковариация оценивается по остаткам на стартовой частоте (2 итерации)."""
    L, R = seg_pair[:, 0], seg_pair[:, 1]
    n = len(L)
    t = np.arange(n) / fs
    r0 = dsp.analyze_tone(R, fs, f_nom, len(R) // 2, nharm=2,
                          band=(20.0, min(20000.0, fs * 0.45)), mains=50.0)
    f0 = float(r0["f0"])                       # старт по чистому каналу
    vL = vR = c = None
    f_best = f0
    for it in range(3):
        DL = np.column_stack([np.sin(2 * np.pi * f_best * t),
                              np.cos(2 * np.pi * f_best * t), np.ones(n)])
        rL = L - DL @ np.linalg.lstsq(DL, L, rcond=None)[0]
        rR = R - DL @ np.linalg.lstsq(DL, R, rcond=None)[0]
        vL, vR = float(np.var(rL)), float(np.var(rR))
        c = float(np.cov(rL, rR)[0, 1])
        if mode == "naive":
            wL, wR = 1.0 / vL, 1.0 / vR
            off = 0.0
        else:
            # GLS: whiten 2-вектора наблюдений матрицей C^{-1/2}
            C = np.array([[vL, c], [c, vR]])
            ev, V = np.linalg.eigh(C)
            W = V @ np.diag(1.0 / np.sqrt(np.maximum(ev, 1e-30))) @ V.T
            wLL, wLR, wRL, wRR = W[0, 0], W[0, 1], W[1, 0], W[1, 1]
            wL = wR = None
            off = (wLL, wLR, wRL, wRR)
        # поиск частоты на сетке вокруг старта + параболическое уточнение.
        # ВАЖНО (v2.18): после вайтнинга W каналы СМЕШИВАЮТСЯ — whitened
        # наблюдение L' содержит вклад тона R с его амплитудой/фазой, поэтому
        # модель обязана иметь 6 параметров (blkdiag(D, D): θ_L и θ_R
        # раздельно), иначе модель не соответствует данным и σ_pred мусорная.
        grid = np.linspace(f0 - 0.05, f0 + 0.05, 11)
        Jv = []
        for f in grid:
            DL = np.column_stack([np.sin(2 * np.pi * f * t),
                                  np.cos(2 * np.pi * f * t), np.ones(n)])
            if mode == "naive":
                rL = L - DL @ np.linalg.lstsq(DL, L, rcond=None)[0]
                rR = R - DL @ np.linalg.lstsq(DL, R, rcond=None)[0]
                J = wL * float(np.sum(rL ** 2)) + wR * float(np.sum(rR ** 2))
            else:
                aLL, aLR, aRL, aRR = off
                y = np.concatenate([aLL * L + aLR * R, aRL * L + aRR * R])
                Dz = np.zeros((2 * n, 6))
                Dz[:n, :3] = aLL * DL
                Dz[:n, 3:] = aLR * DL
                Dz[n:, :3] = aRL * DL
                Dz[n:, 3:] = aRR * DL
                coef, *_ = np.linalg.lstsq(Dz, y, rcond=None)
                J = float(np.sum((y - Dz @ coef) ** 2))
            Jv.append(J)
        i = int(np.argmin(Jv))
        f_best = grid[i]
        if 0 < i < len(grid) - 1:
            denom = Jv[i - 1] - 2 * Jv[i] + Jv[i + 1]
            if denom > 0:
                f_best += (Jv[i - 1] - Jv[i + 1]) / (2 * denom) * (grid[1] - grid[0])
        if mode == "naive":
            break                                   # ковариация не влияет
    # неопределённость частоты — профильный метод (только GLS): RSS(f) на
    # мелкой сетке вокруг минимума; whitened-остатки имеют единичную
    # дисперсию, поэтому var(f̂) = 1/(2·кривизна RSS(f))
    var_f = None
    if mode == "gls":
        aLL, aLR, aRL, aRR = off
        fine = np.linspace(f_best - 0.01, f_best + 0.01, 11)
        rss = []
        for f in fine:
            DL = np.column_stack([np.sin(2 * np.pi * f * t),
                                  np.cos(2 * np.pi * f * t), np.ones(n)])
            y = np.concatenate([aLL * L + aLR * R, aRL * L + aRR * R])
            Dz = np.zeros((2 * n, 6))
            Dz[:n, :3] = aLL * DL
            Dz[:n, 3:] = aLR * DL
            Dz[n:, :3] = aRL * DL
            Dz[n:, 3:] = aRR * DL
            coef, *_ = np.linalg.lstsq(Dz, y, rcond=None)
            rss.append(float(np.sum((y - Dz @ coef) ** 2)))
        i = int(np.argmin(rss))
        if 0 < i < len(fine) - 1:
            denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
            if denom > 0:
                step = fine[1] - fine[0]
                var_f = 1.0 / (denom / (step * step))
    return {"f": f_best, "var_f": var_f, "noise_var_L": vL, "noise_var_R": vR,
            "noise_cov": c, "rho": (c / math.sqrt(vL * vR)
                                    if vL > 0 and vR > 0 else None)}


def ppm_of(f):
    return (f / 1000.0 - 1.0) * 1e6


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calib.Calibration()
    results = []
    print("=== v2.18: сравнение оценщиков на реальных захватах v2.14 ===",
          flush=True)
    for idx in IDX:
        path = SRC / CAPTURES[idx]
        _, cap = wavfile.read(str(path))
        cap = cap.astype(np.float64)
        onset = R_ONSET[idx]
        windows = {
            "production_R_onset": (onset + 0.4, onset + 0.4 + 2.0),
            "fixed_reference": (2.255, 4.255),
        }
        rec = {"index": idx, "level_dbfs": None, "windows": {}}
        src = json.load(open(SRC / "20261114-004425-results.json"))
        rec["level_dbfs"] = next(x["level_dbfs"] for x in src["clock_results"]
                                 if x["index"] == idx)
        for wname, (t0, t1) in windows.items():
            a, b = int(t0 * FS), int(t1 * FS)
            win = cap[a:b]
            entry = {}
            # production-центроид по каналам (production-путь)
            for c, ch in ((0, "L"), (1, "R")):
                r = dsp.analyze_tone(win[:, c], FS, 1000.0, N, nharm=2,
                                     band=(20.0, cal["band_hi"]),
                                     mains=cal["mains"])
                entry[f"prod_{ch}"] = {
                    "ppm": round((r["f0"] / 1000.0 - 1) * 1e6, 3),
                    "f0_hz": round(r["f0"], 4),
                    "level_dbfs": round(r["level_dbfs"], 1),
                    "noise_dbfs": round(r["noise_dbfs"], 1),
                    "carrier_local_snr_db": round(r["local_snr_db"], 1),
                    "coh_err_bins": round(r["coh_err_bins"], 4)}
            # LSQ / phase-slope по каналам
            for c, ch in ((0, "L"), (1, "R")):
                f_lsq = est_lsq(win[:, c], FS, 1000.0)
                f_ph, amp_ph = est_phase_slope(win[:, c], FS, 1000.0)
                entry[f"lsq_{ch}"] = {"ppm": round(ppm_of(f_lsq), 3),
                                      "f_hz": round(f_lsq, 5)}
                entry[f"phase_{ch}"] = {
                    "ppm": (round(ppm_of(f_ph), 3) if f_ph is not None else None),
                    "amp_dbfs": (round(20 * math.log10(max(amp_ph, 1e-12)), 1)
                                 if amp_ph is not None else None),
                    "valid": f_ph is not None}
            # совместные (L+R)
            jn = est_joint(win, FS, 1000.0, "naive")
            jg = est_joint(win, FS, 1000.0, "gls")
            entry["joint_naive"] = {"ppm": round(ppm_of(jn["f"]), 3),
                                    "var_f_hz2": (round(jn["var_f"], 8)
                                                  if jn["var_f"] else None)}
            entry["joint_gls"] = {"ppm": round(ppm_of(jg["f"]), 3),
                                  "var_f_hz2": (round(jg["var_f"], 8)
                                                if jg["var_f"] else None),
                                  "rho_noise": (round(jg["rho"], 3)
                                                if jg["rho"] else None),
                                  "sigma_pred_ppm": (round(jg["var_f"] ** 0.5 / 1000 * 1e6, 3)
                                                     if jg["var_f"] else None)}
            sp = abs(entry["prod_L"]["ppm"] - entry["prod_R"]["ppm"])
            entry["prod_spread_ppm"] = round(sp, 3)
            rec["windows"][wname] = entry
            print(f"[{idx:2d}] {rec['level_dbfs']:+6.0f} {wname}: "
                  f"prod L {entry['prod_L']['ppm']:+7.2f} / R {entry['prod_R']['ppm']:+7.2f} "
                  f"(spread {sp:.2f}) | lsq L {entry['lsq_L']['ppm']:+7.2f} / "
                  f"R {entry['lsq_R']['ppm']:+7.2f} | phase R "
                  f"{entry['phase_R']['ppm'] if entry['phase_R']['ppm'] is not None else '—'} | "
                  f"joint_naive {entry['joint_naive']['ppm']:+7.2f} | "
                  f"joint_gls {entry['joint_gls']['ppm']:+7.2f} "
                  f"(ρ={entry['joint_gls']['rho_noise']}, σ_pred="
                  f"{entry['joint_gls']['sigma_pred_ppm']} ppm)", flush=True)
        results.append(rec)
    json.dump({"truth_ppm": TRUTH_PPM, "results": results},
              open(OUT / "v218-estimators-real.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v218-estimators-real.json")


if __name__ == "__main__":
    main()
