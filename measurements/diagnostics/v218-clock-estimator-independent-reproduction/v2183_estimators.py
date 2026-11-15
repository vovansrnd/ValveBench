#!/usr/bin/env python3
"""v2.18.3 — оценщики с ИСПРАВЛЕНИЯМИ по ревизии v2.18.2.

est_lsq (перенесён из v2.18.1, ветка 8d8b040 — независимый старт):
  * грубая оценка — пик оконной периодограммы ±2 Гц (НЕ центроид);
  * децимация ТОЛЬКО если fs > 96 кГц (48 кГц — без децимации;
    двойная децимация v2.18.2 устранена);
  * МНК-сетка ±0.15 Гц + ПАРАБОЛА С ОГРАНИЧЕНИЯМИ (ревизия v2.18.2 п.1):
    только вогнутый пик (кривизна > 0), смещение ограничено соседними
    бинами (|δ| ≤ step) — парабола не может улететь за сетку;
  * возвращает (f_est, fit_rate_hz) — фактическая частота фита
    сохраняется в результатах (требование ревизии).

ppm_of_pilot(f_est, pilot): ошибка относительно ПИЛОТА
  (исправление ревизии v2.18.1: ppm_of() делил на 1000 Гц для всех
  пилотов — −3000/+13000 ppm были разницей номиналов).

Playback НЕТ; production только импортируется.
"""
import math

import numpy as np
from scipy.signal import resample_poly


def est_lsq(seg, fs, f_nom, coarse_hz=2.0):
    """Нелинейный МНК-фит синусоиды с независимым стартом.

    Возвращает (f_est, fit_rate_hz). Парабола ограничена: только
    вогнутый пик (denom > 0 для максимума периодограммы / выпуклость
    вниз для RSS-минимума), |смещение| ≤ step (внутри соседних бинов)."""
    if fs > 96000:                       # 192 кГц → 48 кГц ОДИН раз
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    fit_rate = fs                        # фактическая частота фита
    # --- независимая грубая оценка: пик периодограммы (Ханн) ±coarse_hz
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.arange(len(X)) * (fs / n)
    band = (freqs >= f_nom - coarse_hz) & (freqs <= f_nom + coarse_hz)
    ib = np.where(band)[0]
    k = int(ib[int(np.argmax(X[ib]))])
    # парабола по модулю: ТОЛЬКО вогнутый пик (y0+y2−2y1 > 0),
    # |смещение| ≤ 0.5 бина (внутри соседних бинов)
    if 0 < k < len(X) - 1:
        y0, y1, y2 = X[k - 1], X[k], X[k + 1]
        curv = y0 + y2 - 2 * y1
        if curv > 0:
            k_f = k + np.clip((y0 - y2) / (2 * curv), -0.5, 0.5)
        else:
            k_f = float(k)
    else:
        k_f = float(k)
    f0 = k_f * fs / n
    # --- МНК-уточнение ±0.15 Гц (шаг 0.0125 Гц) + ограниченная парабола
    # по RSS: минимум → кривизна > 0, |смещение| ≤ step
    grid = np.linspace(f0 - 0.15, f0 + 0.15, 25)
    step = grid[1] - grid[0]
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    f_best = grid[i]
    if 0 < i < len(grid) - 1:
        curv2 = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if curv2 > 0:                    # вогнутость вниз (минимум)
            # вершина параболы = (y0−y2)·step/(2·curv2); в v2.18.3-черновике
            # множитель step был пропущен (юнит-баг: смещение в шагах
            # трактовалось как Гц) + кламп ±step → f_est смещался ровно на
            # ±1 шаг (12.5 ppm) — систематическая ошибка всех LSQ-оценок
            offset = np.clip((rss[i - 1] - rss[i + 1]) / (2 * curv2) * step,
                             -step, step)
            f_best += offset
    return f_best, fit_rate


def ppm_of_pilot(f_est, pilot):
    return (f_est / pilot - 1.0) * 1e6


def est_joint(seg_pair, fs, f_nom):
    """Совместная GLS-оценка (общая частота, отдельные амплитуды/фазы L/R,
    ковариация по остаткам). Возвращает (f_est, var_f, fit_rate_hz).
    Ограничение (ревизия v2.18.2): whitening учитывает только мгновенную
    2×2 ковариацию L/R (нулевой межканальный сдвиг), не частотную
    структуру — σ_pred условна для этой модели."""
    L, R = seg_pair[:, 0], seg_pair[:, 1]
    n = len(L)
    t = np.arange(n) / fs
    f0, _ = est_lsq(R, fs, f_nom)          # старт по чистому каналу
    vL = vR = c = None
    f_best = f0
    off = None
    for it in range(3):
        DL = np.column_stack([np.sin(2 * np.pi * f_best * t),
                              np.cos(2 * np.pi * f_best * t), np.ones(n)])
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
            D = np.column_stack([np.sin(2 * np.pi * f * t),
                                 np.cos(2 * np.pi * f * t), np.ones(n)])
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
    # σ_pred: var(f̂) = 2σ²/RSS'' (множитель 2, вывод logL = −RSS/2σ²)
    fine = np.linspace(f_best - 0.01, f_best + 0.01, 11)
    rss = []
    aLL, aLR, aRL, aRR = off
    for f in fine:
        D = np.column_stack([np.sin(2 * np.pi * f * t),
                             np.cos(2 * np.pi * f * t), np.ones(n)])
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
    if 0 < i < len(fine) - 1:
        denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom > 0:
            step = fine[1] - fine[0]
            var_f = 2.0 / (denom / (step * step))       # множитель 2!
    return f_best, var_f, fs
