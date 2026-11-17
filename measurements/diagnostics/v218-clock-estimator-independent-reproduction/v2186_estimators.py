#!/usr/bin/env python3
"""v2.18.6 — line-aware оценщик с КАЛИБРОВАННЫМ QC захвата.

Ревизия v2.18.5 (COORDINATOR-REVIEW-v2185.md, журнал §20.7):
  п.2 — QC «max/медиана» заменён на калиброванный детектор линий:
    * ЛОКАЛЬНЫЙ пол (скользящая медиана 161 бин ≈ ±40 Гц) вместо
      медианы всего спектра — цветной шум со наклоном не даёт
      ложных срабатываний;
    * УСТОЙЧИВОСТЬ: линия = ≥3 соседних бинов над локальным полом
      (случайный максимум белого шума не образует кластер: вероятность
      трёх подряд бинов >10 дБ над полом ~1e-9 на позицию);
    * дБc ОТНОСИТЕЛЬНО НЕСУЩЕЙ (порог −35 дБc) вместо отношения к
      медиане остатка — чистый синус с нулевым остатком больше не
      флагается числовым мусором (плюс прямой обход: остаток
      пренебрежим (< −60 дБ от несущей) → status='ok' без анализа);
    * зона исключения вокруг несущей СУЖЕНА до ±1.5 Гц (структурная
      утечка неидеального фита) — прежние ±5 Гц МАСКИРОВАЛИ ровно тот
      сценарий, который надо ловить (пилот 997 + помеха 1000 Гц,
      разнос 3 Гц, ошибка −127 ppm в v2.18.4);
    * НОРМАЛЬНЫЙ путь ok обязан работать: чистый синус, белый и
      цветной шум → ok; TP/FP/TN/FN публикуются
      (v2186_qc_confusion.py).
  п.1 — конвенция CSD: scipy.signal.csd(L, R) = conj(FFT(L))·FFT(R);
    для синтеза R = g·L с измеренным кросс-спектром требуется
    g = S_LR/S_LL БЕЗ сопряжения (в v2.18.5 сопряжение давало
    зеркальную фазу, а verifier FL·conj(FR) с противоположной
    конвенцией компенсировал её — совпадение было кажущимся);
    end-to-end тест с известной фазой +0.37 рад — v2186_unit_tests.py.
  п.3 — воспроизведение контрпримера −127 ppm: контрпример возникал
    переносом coarse-пика в фит. v2.18.6: фит ЯКОРИТСЯ на известный
    номинал (±0.15 Гц = ±150 ppm), развёртка ±coarse_hz — только
    метаданные (coarse_edge); захват помехи фит-окном невозможен,
    помеха на ≥2.5 Гц остаётся в остатке и НАЗЫВАЕТСЯ QC
    (line_hz/line_dbc). Смещения помехи ±3/±5 Гц — TP с атрибуцией.
  п.5 — est_joint проверяет ОБА канала: старт из валидного канала с
    лучшим локальным SNR; отказ только при отказе ОБОИХ; при одном
    валидном — статус *_partial (маскировка v2.18.4: шумный L не
    должен хоронить пригодный R).

Playback НЕТ; production только импортируется.
"""
import math

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import resample_poly

# Калиброванные параметры line-QC (подбор и TP/FP/TN/FN —
# v2186_qc_confusion.py; менять вместе с ним)
QC_CFG = {
    "excl_hz": 1.5,      # зона структурной утечки фита вокруг f_est
    "edge_hz": 20.0,     # отступ от DC и Найквиста
    "floor_bins": 161,   # окно локальной медианы (±40 Гц при 0.5 Гц/бин)
    "thresh_db": 10.0,   # бин над локальным полом
    "min_bins": 3,       # устойчивость линии: ≥3 соседних бинов
    "dbc_min": -35.0,    # минимальная значимость линии относительно
                         # несущей: шумовые максимумы ≈ −62 дБc при
                         # −96 дБFS; FP-хвост цветного MC (8/1500) лежал
                         # в −39.4…−37.9 дБc при пороге −40 → порог −35;
                         # слабейшая протестированная помеха +12 дБc
    "resid_skip_db": -60.0,  # остаток ниже −60 дБ от несущей → ok
}


def parabolic_refine(y0, y1, y2):
    """Параболическое уточнение ВЕРШИНЫ периодограммы (v2.18.5,
    детерминированные тесты — v2186_unit_tests.py): вогнутый максимум
    (curv < 0) → смещение ±0.5 бина; плоская/выпуклая — не применяется.
    Возвращает (offset_bins, applied)."""
    curv = y0 + y2 - 2 * y1
    if curv < 0:
        return float(np.clip((y0 - y2) / (2 * curv), -0.5, 0.5)), True
    return 0.0, False


def fine_refine(rss, grid):
    """Уточнение по минимуму RSS: curv2 > 0, смещение ±step; минимум на
    краю сетки → at_edge=True → статус grid_edge.
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


def residual_line_qc(resid, fs, f_est, carrier_amp, cfg=QC_CFG):
    """Детектор линий в остатке (ревизия v2.18.5 п.2).

    Возвращает словарь: {line: bool, line_hz, line_dbc, line_bins}.
    Критерии линии: кластер ≥min_bins соседних бинов, каждый ≥thresh_db
    над ЛОКАЛЬНЫМ полом (медиана floor_bins), вне ±excl_hz от f_est,
    пик кластера ≥dbc_min дБc относительно несущей."""
    n = len(resid)
    win = np.hanning(n)
    Wr = np.abs(np.fft.rfft(resid * win))
    df = fs / n
    fr = np.arange(len(Wr)) * df
    out = {"line": False, "line_hz": None, "line_dbc": None,
           "line_bins": 0}
    carrier_ref = carrier_amp * win.sum() / 2.0
    if carrier_ref <= 0:
        return out
    floor_loc = median_filter(Wr, size=cfg["floor_bins"], mode="nearest")
    band = ((fr > cfg["edge_hz"]) & (fr < fs / 2 - cfg["edge_hz"]) &
            (np.abs(fr - f_est) > cfg["excl_hz"]))
    above = (Wr > 10 ** (cfg["thresh_db"] / 20.0) * floor_loc) & band
    if not above.any():
        return out
    # кластеры подряд идущих бинов
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
    калиброванным line-aware QC. Словарь QC:
    f_est, fit_rate_hz, status (ok/grid_edge/line_contaminated),
    coarse_bin_rel, coarse_peak_hz, coarse_edge (развёртка ±coarse_hz —
    метаданные утечки линии в полосу, НЕ источник фита),
    line_hz, line_dbc, line_bins, snr_local_db."""
    if fs > 96000:
        seg = resample_poly(seg, 1, 4)
        fs = fs / 4
    n = len(seg)
    t = np.arange(n) / fs
    fit_rate = fs
    out = {"fit_rate_hz": fit_rate, "status": "ok", "coarse_bin_rel": None,
           "coarse_peak_hz": None, "line_hz": None, "line_dbc": None,
           "line_bins": 0, "snr_local_db": None}

    # --- грубая развёртка ±coarse_hz: QC-МЕТАДАННЫЕ, не источник фита.
    # АРХИТЕКТУРА v2.18.6: пилот ИЗВЕСТЕН (номинал f_nom), фит якорится
    # на номинал ±0.15 Гц (±150 ppm при 1 кГц — диапазон ухода такта
    # DUT). Перенос coarse-пика в фит ИСКЛЮЧЁН ПО КОНСТРУКЦИИ — механизм
    # «868 Гц» (teleport coarse-пика утечкой линии + невалидная
    # парабола + неограниченное смещение) невозможен. Развёртка
    # сохраняется как диагностический признак: coarse_edge=True (argmax
    # на границе полосы) сигнализирует об утечке линии в полосу.
    win = np.hanning(n)
    X = np.abs(np.fft.rfft(seg * win))
    freqs = np.arange(len(X)) * (fs / n)
    ib = np.where((freqs >= f_nom - coarse_hz) &
                  (freqs <= f_nom + coarse_hz))[0]
    k = int(ib[int(np.argmax(X[ib]))])
    out["coarse_bin_rel"] = round((k - ib[0]) / (ib[-1] - ib[0]), 3)
    out["coarse_peak_hz"] = round(freqs[k], 3)
    out["coarse_edge"] = bool(k == ib[0] or k == ib[-1])
    # --- МНК-фит ОТ НОМИНАЛА ±0.15 Гц + парабола (минимум RSS).
    # Широкий RSS-поиск по полосе ±2 Гц НЕ используется: при сильной
    # ближней помехе фит на краю полосы ЧАСТИЧНО ПОГЛОЩАЕТ помеху
    # (спектральное перекрытие Ханна на <1 Гц почти полное) и RSS
    # падает к краю — захват помехи. Окно ±0.15 Гц безопасно: помеха
    # на ≥2.5 Гц от номинала попадает в боковые лепестки (−31 дБ и
    # ниже) и остаётся в остатке для QC.
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
    # --- амплитуда/фаза/DC при f_est (финальный фит)
    D = np.column_stack([np.sin(2 * math.pi * f_est * t),
                         np.cos(2 * math.pi * f_est * t), np.ones(n)])
    coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
    resid = seg - D @ coef
    amp = math.hypot(coef[0], coef[1])
    out["snr_local_db"] = (round(20 * math.log10(amp /
                          max(float(np.sqrt(np.mean(resid ** 2))), 1e-12)), 1)
                          if amp > 0 else None)
    # --- line-QC на осмысленном остатке (ok И grid_edge). При grid_edge
    # ландшафт RSS повреждён (градиент поглощения помехи при сильной
    # ближней линии — провал пилота << вариаций ландшафта): фит
    # отказывает, а QC по остатку НАЗЫВАЕТ помеху (line_hz/line_dbc) —
    # отказ с атрибуцией вместо немого grid_edge
    if out["status"] in ("ok", "grid_edge") and amp > 0:
        resid_rms = float(np.sqrt(np.mean(resid ** 2)))
        if resid_rms < 10 ** (cfg["resid_skip_db"] / 20.0) * amp:
            pass                             # остаток пренебрежим → ok
        else:
            qc = residual_line_qc(resid, fs, f_est, amp, cfg)
            out["line_hz"] = qc["line_hz"]
            out["line_dbc"] = qc["line_dbc"]
            out["line_bins"] = qc["line_bins"]
            if qc["line"]:
                out["status"] = "line_contaminated"
    out["fit_rate_hz"] = fit_rate
    return out


def est_joint(seg_pair, fs, f_nom):
    """Совместная GLS (общая частота, отдельные амплитуды/фазы L/R).
    σ_pred = var(f̂) = 2σ²/RSS'' (множитель 2). Ограничение: whitening
    только мгновенной 2×2 ковариации (нулевой межканальный сдвиг).

    ОБА КАНАЛА (ревизия v2.18.5 п.5): QC запускается на L и R; старт —
    из валидного канала с лучшим локальным SNR (маскировка v2.18.4:
    контаминированный L не должен хоронить пригодный R). Отказ
    (None, None, fs, status) только при невалидности ОБОИХ; при одном
    валидном статус получает суффикс _partial."""
    L, R = seg_pair[:, 0], seg_pair[:, 1]
    n = len(L)
    t = np.arange(n) / fs
    qcL = est_lsq(L, fs, f_nom)
    qcR = est_lsq(R, fs, f_nom)
    valid = [(q, ch) for q, ch in ((qcL, "L"), (qcR, "R"))
             if q["status"] == "ok" and q.get("f_est") is not None]
    partial = len(valid) == 1
    if not valid:
        status0 = qcL["status"] if qcL["status"] != "ok" else qcR["status"]
        return None, None, fs, status0
    q0, _ = max(valid, key=lambda tq: (tq[0].get("snr_local_db")
                                       if tq[0].get("snr_local_db")
                                       is not None else -1e9))
    f0 = q0["f_est"]
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
    if partial:
        status += "_partial"
    return f_best, var_f, fs, status


def rms_dbfs(x):
    """Production-метрика dsp.rms_dbfs: RMS × √2 относительно полного синуса."""
    rms = float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return 20 * math.log10(max(rms * math.sqrt(2.0), 1e-12))
