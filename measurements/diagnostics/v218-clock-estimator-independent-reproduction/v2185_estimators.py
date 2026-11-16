#!/usr/bin/env python3
"""v2.18.5 — line-aware оценщик частоты (исправления ревизий v2.18.2-4).

est_lsq возвращает СЛОВАРЬ с полным QC (архитектура координатора:
«оценщик должен возвращать не только частоту, но и признак успешного
захвата, положение coarse-пика, расстояние до границы полосы, SNR и
неопределённость»):

  f_est, fit_rate      — оценка частоты и фактическая частота фита;
  status               — ok / edge_peak / grid_edge / line_contaminated;
  coarse_bin_rel       — положение coarse-пика внутри полосы поиска
                         (0 = левый край, 1 = правый край);
  line_hz, line_amp_db — частота и уровень сильнейшей посторонней линии
                         в остатке (если line_contaminated);
  snr_local_db         — локальный SNR несущей.

Исправления:
  * кривизна: уточнение coarse-пика только при ВОГНУТОСТИ (curv < 0 для
    максимума — в v2.18.4 было curv > 0, уточнение выполнялось на
    минимумах и пропускалось на нормальных пиках — ревизия v2.18.4 п.1);
  * boundary-guard: coarse-argmax сравнивается с ГРАНИЦАМИ ИНТЕРВАЛА
    поиска ib (§18.3: прежний код сравнивал с границами всего FFT и
    использовал соседний бин ВНЕ интервала для интерполяции);
  * RSS-минимум на краю локальной сетки → status grid_edge;
  * параболические уточнения вынесены в тестируемые хелперы
    parabolic_refine / fine_refine (детерминированные юнит-тесты
    вогнутого/плоского/выпуклого пика, внерешёточного смещения,
    ограничения ±0.5 бина и ±step — ревизия v2.18.4 п.1);
  * est_joint FAIL-CLOSED: невалидный старт (edge_peak/grid_edge/
    line_contaminated) → (None, None, fs, status), не номинал
    (ревизия v2.18.4 п.3); собственный минимум RSS на краю тонкой
    сетки → статус grid_edge;
  * post-fit line QC: остаток после фита анализируется — сильнейшая
    линия вне ±5 Гц от f_est; если она > 10 дБ над локальным полом
    остатков → line_contaminated (находка v2.18.4: линия −30 дБFS
    смещала оценку на −127 ppm без edge-сигнатуры);
  * децимация только при fs > 96 кГц; фит 48 кГц.

Playback НЕТ; production только импортируется.
"""
import math

import numpy as np
from scipy.signal import resample_poly


def parabolic_refine(y0, y1, y2):
    """Параболическое уточнение ВЕРШИНЫ периодограммы (ревизия v2.18.4
    п.1; детерминированные тесты — v2185_unit_tests.py):
      * вогнутый максимум (curv < 0) → смещение, ОГРАНИЧЕННОЕ ±0.5 бина;
      * плоская (curv = 0) и выпуклая (curv > 0) формы → уточнение НЕ
        применяется. В v2.18.1 (8d8b040) уточнение выполнялось при любом
        знаке знаменателя, а смещение было неограниченным — механизм
        «868 Гц» (экстраполяция по возрастающему склону).
    Возвращает (offset_bins, applied)."""
    curv = y0 + y2 - 2 * y1
    if curv < 0:
        return float(np.clip((y0 - y2) / (2 * curv), -0.5, 0.5)), True
    return 0.0, False


def fine_refine(rss, grid):
    """Уточнение по минимуму RSS (ревизия v2.18.4 п.1): парабола валидна
    только при curv2 > 0 (минимум), смещение ограничено ±step (для
    неотрицательных RSS |offset| ≤ 0.5·step — инвариант покрывается
    тестом); минимум на КРАЮ сетки → at_edge=True → статус grid_edge.
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


def est_lsq(seg, fs, f_nom, coarse_hz=2.0, qc_line_db=10.0):
    """Независимый LSQ с line-aware QC. Возвращает словарь QC."""
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    fit_rate = fs
    out = {"fit_rate_hz": fit_rate, "status": "ok", "coarse_bin_rel": None,
           "line_hz": None, "line_amp_db": None, "snr_local_db": None}

    # --- независимая грубая оценка: пик периодограммы (Ханн) ±coarse_hz
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.arange(len(X)) * (fs / n)
    ib = np.where((freqs >= f_nom - coarse_hz) &
                  (freqs <= f_nom + coarse_hz))[0]
    k = int(ib[int(np.argmax(X[ib]))])
    # boundary-guard (§18.3): argmax на ГРАНИЦЕ ИНТЕРВАЛА поиска
    if k == ib[0] or k == ib[-1]:
        out["status"] = "edge_peak"
        out["coarse_bin_rel"] = (0.0 if k == ib[0] else 1.0)
        out["coarse_peak_hz"] = round(freqs[k], 3)
        return out                          # отказ без дальнейшего фита
    out["coarse_bin_rel"] = round((k - ib[0]) / (ib[-1] - ib[0]), 3)
    out["coarse_peak_hz"] = round(freqs[k], 3)
    # --- уточнение coarse-пика: только ВОГНУТЫЙ максимум (curv < 0)
    if 0 < k < len(X) - 1:
        off, applied = parabolic_refine(X[k - 1], X[k], X[k + 1])
        k_f = k + (off if applied else 0.0)
    else:
        k_f = float(k)
    f0 = k_f * fs / n
    # --- МНК-уточнение ±0.15 Гц + парабола (минимум RSS: curv2 > 0)
    grid = np.linspace(f0 - 0.15, f0 + 0.15, 25)
    step = grid[1] - grid[0]
    rss = []
    coefs = []
    for f in grid:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
        coefs.append(coef)
    f_best, at_edge = fine_refine(rss, grid)
    if at_edge:
        out["status"] = "grid_edge"          # RSS-минимум на краю сетки
    f_est = f_best
    out["f_est"] = f_est
    # --- амплитуда/фаза/DC при f_est (финальный фит)
    D = np.column_stack([np.sin(2 * math.pi * f_est * t),
                         np.cos(2 * math.pi * f_est * t), np.ones(n)])
    coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
    resid = seg - D @ coef
    amp = math.hypot(coef[0], coef[1])
    # локальный SNR: амплитуда тона против RMS остатка
    out["snr_local_db"] = (round(20 * math.log10(amp /
                          max(float(np.sqrt(np.mean(resid ** 2))), 1e-12)), 1)
                          if amp > 0 else None)
    # --- post-fit line QC: остаток → спектр → сильнейшая линия вне ±5 Гц
    if out["status"] == "ok":
        Wr = np.abs(np.fft.rfft(resid * np.hanning(n)))
        fr = np.arange(len(Wr)) * (fs / n)
        mask = (np.abs(fr - f_est) > 5.0) & (fr > 20.0) & (fr < fs / 2 - 20)
        if np.any(mask):
            floor = float(np.median(Wr[mask]))
            j_rel = int(np.argmax(np.where(mask, Wr, 0.0)))
            line_amp = float(Wr[j_rel])
            if floor > 0 and line_amp / floor > 3.16:      # > 10 дБ над полом
                out["status"] = "line_contaminated"
                out["line_hz"] = round(fr[j_rel], 2)
                out["line_amp_db"] = round(20 * math.log10(max(line_amp, 1e-12)), 1)
    out["f_est"] = f_est
    out["fit_rate_hz"] = fit_rate
    return out


def est_joint(seg_pair, fs, f_nom):
    """Совместная GLS-оценка (общая частота, отдельные амплитуды/фазы L/R).
    σ_pred = var(f̂) = 2σ²/RSS'' (множитель 2). Ограничение: whitening
    только мгновенной 2×2 ковариации (нулевой межканальный сдвиг).

    FAIL-CLOSED (ревизия v2.18.4 п.3): невалидный старт одиночного
    канала (edge_peak / grid_edge / line_contaminated) НЕ потребляется
    молча — возвращается (None, None, fs, status) с унаследованным
    статусом; вызывающий обязан обработать отказ. Успех:
    (f_best, var_f, fs, 'ok'|'grid_edge') — grid_edge, если минимум RSS
    на краю тонкой сетки (var_f = None)."""
    L, R = seg_pair[:, 0], seg_pair[:, 1]
    n = len(L)
    t = np.arange(n) / fs
    qc0 = est_lsq(R, fs, f_nom)
    f0 = qc0.get("f_est")
    status0 = qc0.get("status", "edge_peak")
    if status0 != "ok" or f0 is None:
        # fail-closed: совместный GLS не запускается от невалидного старта
        return None, None, fs, status0
    off = None
    f_best = f0
    for it in range(3):
        DL = np.column_stack([np.sin(2 * math.pi * f_best * t),
                              np.cos(2 * math.pi * f_best * t), np.ones(n)])
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
                                 np.cos(2 * math.pi * f * t), np.ones(n)])
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
                f_best += np.clip((Jv[i - 1] - Jv[i + 1]) / (2 * denom) * step,
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
        status = "grid_edge"          # минимум RSS на краю тонкой сетки
    else:
        denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom > 0:
            step = fine[1] - fine[0]
            var_f = 2.0 / (denom / (step * step))       # множитель 2
    return f_best, var_f, fs, status


def rms_dbfs(x):
    """Production-метрика dsp.rms_dbfs: RMS × √2 относительно полного синуса."""
    rms = float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return 20 * math.log10(max(rms * math.sqrt(2.0), 1e-12))
