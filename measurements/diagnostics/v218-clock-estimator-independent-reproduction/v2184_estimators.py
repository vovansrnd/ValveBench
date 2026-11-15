#!/usr/bin/env python3
"""v2.18.4 — оценщики с отказом при coarse-пике на границе поисковой полосы
(ревизия v2.18.3 п.2: «Добавить явный отказ при coarse-пике на границе
поисковой полосы и проверить сильную линию за пределами диапазона»).

Находка v2.18.4 (воспроизведение 868 Гц): coarse-argmax на ГРАНИЦЕ полосы
поиска + ВОЗРАСТАЮЩИЕ соседи (утечка сильной линии за пределами полосы) +
неограниченная парабола = смещение −133 бина → оценка 866 Гц вместо 997.
Здесь: est_lsq возвращает (f_est, fit_rate, status); status='edge_peak'
если coarse-argmax на первом/последнем бине полосы поиска — захват
ненадёжен, вызывающий решает (отказ или предупреждение).

White-модель: уровни по ПРОИЗВОДСТВЕННОЙ метрике rms_dbfs() (RMS × √2
относительно полного синуса): генератор σ = 10^(dBFS/20)/√2, тогда
rms_dbfs(шум) = заявленный уровень. Playback НЕТ; production только
импортируется.
"""
import math

import numpy as np
from scipy.signal import resample_poly


def rms_dbfs(x):
    """Production-метрика dsp.rms_dbfs: RMS × √2 относительно полного синуса."""
    rms = float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return 20 * math.log10(max(rms * math.sqrt(2.0), 1e-12))


def est_lsq(seg, fs, f_nom, coarse_hz=2.0):
    """Независимый МНК-фит (v2.18.3) + отказ при coarse-пике на границе.

    Возвращает (f_est, fit_rate, status):
      status='ok'        — coarse-пик внутри полосы (не на границе);
      status='edge_peak' — coarse-argmax на первом/последнем бине полосы
                           поиска: вероятна сильная линия ЗА пределами
                           полосы (утечка к границе) — захват ненадёжен.
    Децимация только при fs > 96 кГц; фит 48 кГц; парабола ограничена."""
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    fit_rate = fs
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.arange(len(X)) * (fs / n)
    band = (freqs >= f_nom - coarse_hz) & (freqs <= f_nom + coarse_hz)
    ib = np.where(band)[0]
    k = int(ib[int(np.argmax(X[ib]))])
    status = "ok"
    if k == ib[0] or k == ib[-1]:
        status = "edge_peak"                # явный отказ (ревизия п.2)
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
    grid = np.linspace(f0 - 0.15, f0 + 0.15, 25)
    step = grid[1] - grid[0]
    rss = []
    for f in grid:
        D = np.column_stack([np.sin(2 * math.pi * f * t),
                             np.cos(2 * math.pi * f * t), np.ones(n)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        rss.append(float(np.sum((seg - D @ coef) ** 2)))
    i = int(np.argmin(rss))
    f_best = grid[i]
    if 0 < i < len(grid) - 1:
        curv2 = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if curv2 > 0:
            offset = np.clip((rss[i - 1] - rss[i + 1]) / (2 * curv2) * step,
                             -step, step)
            f_best += offset
    return f_best, fit_rate, status


def est_joint(seg_pair, fs, f_nom):
    """Совместная GLS (общая частота, отдельные амплитуды/фазы L/R).
    σ_pred = var(f̂) = 2σ²/RSS'' (множитель 2). Ограничение: whitening
    только мгновенной 2×2 ковариации (нулевой межканальный сдвиг)."""
    L, R = seg_pair[:, 0], seg_pair[:, 1]
    n = len(L)
    t = np.arange(n) / fs
    f0, _, _ = est_lsq(R, fs, f_nom)
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
    if 0 < i < len(fine) - 1:
        denom = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if denom > 0:
            step = fine[1] - fine[0]
            var_f = 2.0 / (denom / (step * step))      # множитель 2
    return f_best, var_f, fs
