#!/usr/bin/env python3
"""v2.18.7 — изоляция каналов, контроль наличия тона, честная
неопределённость (журнал §21, ревизия v2.18.6).

Три исправления против COORDINATOR-REVIEW-v2186.md:

1. TRUE partial isolation: в v2.18.6 `ok_partial` выбирал старт из
   валидного канала, но включал ОБА канала в ковариацию, whitening и
   совместный RSS — загрязнённый канал участвовал в оценке. Теперь:
   оба валидны → совместный GLS; валиден один → результат ТОЛЬКО этого
   канала (точное совпадение с его est_lsq — допуск 0); оба невалидны →
   отказ. В результате — `channels_used` и статусы каждого канала.

2. Tone-presence gate: `snr_local_db` в v2.18.6 был информационным —
   контрольная проверка координатора: шум −80 дБFS без пилота →
   `ok` в 55/200. Теперь carrier-local SNR (выровнен с production
   `measure.py:CLOCK_MIN_SNR_DB = 20.0`: мощность несущей против
   локального пола остатка в полосе ±32 Гц вокруг f_est, исключая
   ±2 Гц): snr < 0 дБ → `tone_missing` (амплитуда фита статистически
   неотличима от проекции шума ≈ −44 дБ при n=96000); 0…20 дБ →
   `snr_low`; ≥ 20 дБ → line-QC. Ни один сигнал без полезного тона
   не получает `ok` и не участвует в joint GLS.

3. Неопределённость: в v2.18.6 est_joint возвращал var_f = 2/RSS''
   (на whitened-данных σ'² = 1 по построению — формула var = 2σ²/RSS''
   верна), но MC добавлял ещё √2 (sqrt(2·var_f)) — двойной множитель.
   Теперь est_lsq/est_joint возвращают `variance_hz2` (дисперсия, Гц²)
   и `sigma_hz = sqrt(variance_hz2)` — преобразование variance → sigma
   выполнено РОВНО ОДИН раз; MC использует sigma_hz напрямую.

Playback НЕТ; production только импортируется.
"""
import math

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import resample_poly

# Калиброванные параметры line-QC и гейта (матрицы TP/FP/TN/FN —
# v2187_qc_sweep.py; менять вместе с ним)
QC_CFG = {
    "excl_hz": 1.5,      # зона структурной утечки фита вокруг f_est
    "edge_hz": 20.0,     # отступ от DC и Найквиста
    "floor_bins": 161,   # окно локальной медианы (±40 Гц при 0.5 Гц/бин)
    "thresh_db": 10.0,   # бин над локальным полом
    "min_bins": 3,       # устойчивость линии: ≥3 соседних бинов
    "dbc_min": -35.0,    # значимость линии относительно несущей
                         # (калибровка v2.18.6: FP-хвост цветного MC
                         # лежал в −39.4…−37.9 дБc при −40)
    "resid_skip_db": -60.0,  # остаток ниже −60 дБ от несущей → line-QC
                             # не запускается (пылевой пол)
    # --- tone-presence gate (НОВОЕ, §21 п.2) ---
    "snr_local_half_hz": 32.0,   # полоса carrier-local SNR (±32 Гц)
    "snr_local_gap_hz": 2.0,     # исключение вокруг несущей
    "snr_min_db": 20.0,          # = production measure.py:26
                                 # CLOCK_MIN_SNR_DB = 20.0 (инженерный
                                 # floor); ниже → snr_low
    "tone_missing_db": 0.0,      # ниже 0 дБ: амплитуда фита на уровне
                                 # проекции шума → tone_missing
}


def parabolic_refine(y0, y1, y2):
    """Параболическое уточнение ВЕРШИНЫ периодограммы (регресс
    v2.18.5/6): вогнутый максимум (curv < 0) → смещение ±0.5 бина;
    плоская (curv = 0) и выпуклая (curv > 0) формы — НЕ применяется
    (механизм «868 Гц»). Возвращает (offset_bins, applied)."""
    curv = y0 + y2 - 2 * y1
    if curv < 0:
        return float(np.clip((y0 - y2) / (2 * curv), -0.5, 0.5)), True
    return 0.0, False


def fine_refine(rss, grid):
    """Уточнение по минимуму RSS: парабола при curv2 > 0, смещение
    ±step; минимум на КРАЮ сетки → at_edge=True → статус grid_edge.
    Возвращает (f_best, at_edge)."""
    i = int(np.argmin(rss))
    if i == 0 or i == len(rss) - 1:
        return grid[i], True
    curv2 = rss[i - 1] - 2 * rss[i] + rss[i + 1]
    if curv2 > 0:
        step = grid[1] - grid[0]
        off = float(np.clip((rss[i - 1] - rss[i + 1]) / (2 * curv2) * step,
                            -step, step))
        return grid[i] + off, False
    return grid[i], False


def residual_line_qc(Wr, fr, carrier_ref, f_est, fs, cfg=QC_CFG):
    """Детектор линий в остатке (v2.18.6, пороги сохранены). Спектр
    остатка Wr/fr передаётся готовым (переиспользуется tone-гейтом).
    carrier_ref = amp·Σw/2 — |FFT| несущей на пиковом бине."""
    out = {"line": False, "line_hz": None, "line_dbc": None,
           "line_bins": 0}
    if carrier_ref <= 0:
        return out
    floor_loc = median_filter(Wr, size=cfg["floor_bins"], mode="nearest")
    band = ((fr > cfg["edge_hz"]) & (fr < fs / 2 - cfg["edge_hz"]) &
            (np.abs(fr - f_est) > cfg["excl_hz"]))
    above = (Wr > 10 ** (cfg["thresh_db"] / 20.0) * floor_loc) & band
    if not above.any():
        return out
    edges = np.diff(np.concatenate(([0], above.view(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    best = None
    for s, e in zip(starts, ends):
        if e - s < cfg["min_bins"]:
            continue
        p = s + int(np.argmax(Wr[s:e]))
        dbc = 20 * math.log10(max(Wr[p] / carrier_ref, 1e-12))
        if dbc >= cfg["dbc_min"] and (best is None or dbc > best[1]):
            best = (p, dbc, e - s)
    if best is not None:
        p, dbc, nb = best
        out.update(line=True, line_hz=round(float(fr[p]), 2),
                   line_dbc=round(dbc, 1), line_bins=int(nb))
    return out


def est_lsq(seg, fs, f_nom, coarse_hz=2.0, cfg=QC_CFG):
    """Независимый LSQ, якоренный на ИЗВЕСТНЫЙ номинал пилота, с
    tone-presence gate и line-aware QC. Словарь QC:
      f_est, fit_rate_hz, status (ok / tone_missing / snr_low /
        grid_edge / line_contaminated),
      coarse_bin_rel, coarse_peak_hz, coarse_edge,
      line_hz, line_dbc, line_bins,
      snr_carrier_db (carrier-local, выровнен с production
        CLOCK_MIN_SNR_DB), variance_hz2 (дисперсия оценки, Гц²),
      channels_used не здесь — в est_joint.

    ГЕЙТ (§21 п.2): snr_carrier < tone_missing_db → tone_missing;
    snr_carrier < snr_min_db → snr_low. Ни один статус без полезного
    тона не является ok; f_est в отказных статусах — информационный
    (сырая оценка «как без QC»)."""
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    fit_rate = fs
    out = {"fit_rate_hz": fit_rate, "status": "ok", "coarse_bin_rel": None,
           "coarse_peak_hz": None, "coarse_edge": None, "line_hz": None,
           "line_dbc": None, "line_bins": 0, "snr_carrier_db": None,
           "variance_hz2": None}

    # --- грубая развёртка ±coarse_hz: МЕТАДАННЫЕ, не источник фита
    # (якорный фит v2.18.6: механизм «868» исключён по построению)
    win = np.hanning(n)
    win_sum = float(win.sum())
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.arange(len(X)) * (fs / n)
    ib = np.where((freqs >= f_nom - coarse_hz) &
                  (freqs <= f_nom + coarse_hz))[0]
    k = int(ib[int(np.argmax(X[ib]))])
    out["coarse_bin_rel"] = round((k - ib[0]) / (ib[-1] - ib[0]), 3)
    out["coarse_peak_hz"] = round(freqs[k], 3)
    out["coarse_edge"] = bool(k == ib[0] or k == ib[-1])

    # --- МНК-фит ОТ НОМИНАЛА ±0.15 Гц (±150 ppm при 1 кГц)
    grid = np.linspace(f_nom - 0.15, f_nom + 0.15, 25)
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    f_best, at_edge = fine_refine(rss, grid)
    if at_edge:
        out["status"] = "grid_edge"          # RSS-минимум на краю сетки
    f_est = f_best
    out["f_est"] = f_est

    # --- амплитуда/фаза/DC при f_est; спектр остатка (для гейта и QC)
    D = np.column_stack([np.sin(2 * math.pi * f_est * t),
                         np.cos(2 * math.pi * f_est * t), np.ones(n)])
    coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
    resid = seg - D @ coef
    amp = math.hypot(coef[0], coef[1])
    resid_rms = float(np.sqrt(np.mean(resid ** 2)))
    Wr = np.abs(np.fft.rfft(resid * win))
    fr = np.arange(len(Wr)) * (fs / n)

    # --- variance_hz2 (§21 п.3): var(f̂) = 2σ²/RSS'', σ² = mean(resid²),
    # RSS'' через конечную разность якорной сетки: denom/step²
    i = int(np.argmin(rss))
    if 0 < i < len(grid) - 1:
        denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom > 0 and amp > 0:
            step = grid[1] - grid[0]
            out["variance_hz2"] = (2.0 * resid_rms ** 2 * step * step
                                   / denom)

    # --- carrier-local SNR (§21 п.2): мощность несущей против
    # локального пола остатка в ±half_hz, исключая ±gap_hz.
    # Нормировка: E|X_шум,k|² = σ²·Σw²; эквивалентный rms шума в полосе
    # K бинов = sqrt(floor_med·K·2/(n·Σw²)); несущая = amp.
    loc = ((np.abs(fr - f_est) > cfg["snr_local_gap_hz"]) &
           (np.abs(fr - f_est) <= cfg["snr_local_half_hz"]) &
           (fr > cfg["edge_hz"]) & (fr < fs / 2 - cfg["edge_hz"]))
    K = int(loc.sum())
    if amp > 0 and K > 0:
        floor_med = float(np.median(Wr[loc] ** 2))
        if floor_med > 0:
            noise_rms_band = math.sqrt(
                floor_med * K * 2.0 / (n * float(np.sum(win ** 2))))
            out["snr_carrier_db"] = round(
                20 * math.log10(amp / noise_rms_band), 1)
        else:
            out["snr_carrier_db"] = 300.0   # пылевой остаток: пол = 0
    elif amp > 0:
        out["snr_carrier_db"] = 300.0

    # --- ГЕЙТ наличия тона (до line-QC; отказные статусы сохраняют
    # f_est как информационную «сырую» оценку)
    snr = out["snr_carrier_db"]
    if snr is None:
        out["status"] = "tone_missing"       # амплитуды нет вовсе
        out["fit_rate_hz"] = fit_rate
        return out
    if snr < cfg["tone_missing_db"]:
        out["status"] = "tone_missing"       # ≈ проекция шума (−44 дБ)
        out["fit_rate_hz"] = fit_rate
        return out
    if snr < cfg["snr_min_db"]:
        out["status"] = "snr_low"            # тон есть, но слаб
        out["fit_rate_hz"] = fit_rate
        return out

    # --- line-QC (v2.18.6, работает и на grid_edge-остатках)
    if out["status"] in ("ok", "grid_edge") and amp > 0:
        if resid_rms < 10 ** (cfg["resid_skip_db"] / 20.0) * amp:
            pass                             # остаток пренебрежим → ok
        else:
            carrier_ref = amp * win_sum / 2.0
            qc = residual_line_qc(Wr, fr, carrier_ref, f_est, fs, cfg)
            out["line_hz"] = qc["line_hz"]
            out["line_dbc"] = qc["line_dbc"]
            out["line_bins"] = qc["line_bins"]
            if qc["line"]:
                out["status"] = "line_contaminated"
    out["fit_rate_hz"] = fit_rate
    return out


def est_joint(seg_pair, fs, f_nom):
    """Совместная оценка с ИСТИННОЙ изоляцией каналов (§21 п.1).

    Возвращает СЛОВАРЬ:
      f_est, variance_hz2, sigma_hz (= sqrt(variance_hz2), Гц),
      status (ok / ok_partial / grid_edge / grid_edge_partial /
              tone_missing / snr_low / line_contaminated),
      channels_used (['L','R'] | ['L'] | ['R'] | []),
      qc_L, qc_R (полные словари est_lsq обоих каналов), fs.

    Семантика:
      * оба канала status=ok → совместный GLS по обоим; variance из
        whitened RSS (whitening нормирует остаточную ковариацию 2×2 к
        единичной, поэтому var = 2/RSS''_whitened — σ'² = 1 точно);
      * валиден один → РЕЗУЛЬТАТ ТОЛЬКО ЭТОГО канала (точное совпадение
        с его est_lsq: тот же f_est и variance_hz2 — допуск 0);
        загрязнённый канал НЕ участвует ни в ковариации, ни в RSS;
        статус ok_partial;
      * оба невалидны → отказ (f_est/sigma None, статус — причина).
    """
    L, R = seg_pair[:, 0], seg_pair[:, 1]
    n = len(L)
    t = np.arange(n) / fs
    qcL = est_lsq(L, fs, f_nom)
    qcR = est_lsq(R, fs, f_nom)
    res = {"fs": fs, "qc_L": qcL, "qc_R": qcR, "channels_used": [],
           "f_est": None, "variance_hz2": None, "sigma_hz": None,
           "status": None}
    validL = qcL["status"] == "ok" and qcL.get("f_est") is not None
    validR = qcR["status"] == "ok" and qcR.get("f_est") is not None

    if validL and validR:
        off = None
        f0L, f0R = qcL["f_est"], qcR["f_est"]
        f0 = 0.5 * (f0L + f0R)
        f_best = f0
        for it in range(3):
            DL = np.column_stack([np.sin(2 * math.pi * f_best * t),
                                  np.cos(2 * math.pi * f_best * t),
                                  np.ones(n)])
            rL = L - DL @ np.linalg.lstsq(DL, L, rcond=None)[0]
            rR = R - DL @ np.linalg.lstsq(DL, R, rcond=None)[0]
            vL, vR = float(np.var(rL)), float(np.var(rR))
            c = float(np.cov(rL, rR)[0, 1])
            C = np.array([[vL, c], [c, vR]])
            ev, V = np.linalg.eigh(C)
            W = V @ np.diag(1.0 / np.sqrt(np.maximum(ev, 1e-30))) @ V.T
            aLL, aLR, aRL, aRR = W[0, 0], W[0, 1], W[1, 0], W[1, 1]
            off = (aLL, aLR, aRL, aRR)
            grid = np.linspace(f0 - 0.15, f0 + 0.15, 11)
            Jv = []
            for f in grid:
                D = np.column_stack([np.sin(2 * math.pi * f * t),
                                     np.cos(2 * math.pi * f * t),
                                     np.ones(n)])
                y = np.concatenate([aLL * L + aLR * R, aRL * L + aRR * R])
                Dz = np.zeros((2 * n, 6))
                Dz[:n, :3] = aLL * D
                Dz[:n, 3:] = aLR * D
                Dz[n:, :3] = aRL * D
                Dz[n:, 3:] = aRR * D
                coef, *_ = np.linalg.lstsq(Dz, y, rcond=None)
                Jv.append(float(np.sum((y - Dz @ coef) ** 2)))
            i = int(np.argmin(Jv))
            f_best = grid[i]
            if 0 < i < len(grid) - 1:
                denom = Jv[i - 1] - 2 * Jv[i] + Jv[i + 1]
                if denom > 0:
                    step = grid[1] - grid[0]
                    f_best += np.clip(
                        (Jv[i - 1] - Jv[i + 1]) / (2 * denom) * step,
                        -step, step)
        fine = np.linspace(f_best - 0.01, f_best + 0.01, 11)
        rss = []
        aLL, aLR, aRL, aRR = off
        for f in fine:
            D = np.column_stack([np.sin(2 * math.pi * f * t),
                                 np.cos(2 * math.pi * f * t), np.ones(n)])
            y = np.concatenate([aLL * L + aLR * R, aRL * L + aRR * R])
            Dz = np.zeros((2 * n, 6))
            Dz[:n, :3] = aLL * D
            Dz[:n, 3:] = aLR * D
            Dz[n:, :3] = aRL * D
            Dz[n:, 3:] = aRR * D
            coef, *_ = np.linalg.lstsq(Dz, y, rcond=None)
            rss.append(float(np.sum((y - Dz @ coef) ** 2)))
        i = int(np.argmin(rss))
        var_f = None
        status = "ok"
        if i == 0 or i == len(rss) - 1:
            status = "grid_edge"
        else:
            denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
            if denom > 0:
                step = fine[1] - fine[0]
                # σ'² = 1 точно: whitening нормирует остаточную
                # ковариацию 2×2 к единичной → var = 2/RSS''
                var_f = 2.0 / (denom / (step * step))
        res.update(f_est=f_best, variance_hz2=var_f,
                   sigma_hz=(math.sqrt(var_f) if var_f is not None
                             else None),
                   channels_used=["L", "R"], status=status)
        return res

    if validL != validR:
        q, ch = (qcL, "L") if validL else (qcR, "R")
        # ИЗОЛЯЦИЯ (§21 п.1): только валидный канал; загрязнённый
        # не участвует ни в чём. Результат == est_lsq валидного канала
        # (тот же f_est и variance_hz2 — совпадение точное).
        res.update(f_est=q["f_est"], variance_hz2=q.get("variance_hz2"),
                   sigma_hz=(math.sqrt(q["variance_hz2"])
                             if q.get("variance_hz2") is not None else None),
                   channels_used=[ch], status="ok_partial")
        return res

    # отказ: оба канала невалидны
    res["status"] = qcL["status"] if qcL["status"] != "ok" else \
        qcR["status"]
    return res


def rms_dbfs(x):
    """Production-метрика dsp.rms_dbfs: RMS × √2 относительно полного синуса."""
    rms = float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return 20 * math.log10(max(rms * math.sqrt(2.0), 1e-12))
