"""0.1.12-p1: контроль достоверности ввода/вывода steady (P1).

Три независимых признака достоверности серии:
* XRUN-контроль по stderr aplay/arecord (underrun воспроизведения / overrun записи);
* контроль фактической длины записи против ожидаемой;
* определение фактического начала воспроизведения по известной форме огибающей
  тест-сигнала (не порогом по амплитуде, а подгонкой фазы трапеции fade-in).

Принципы:
* отсутствие XRUN-сообщений — не доказательство отсутствия всех аппаратных сбоев;
  контролируется только то, что видно на уровне stderr, длины записи и огибающей;
* измеренные диапазоны состояний сдвигаются на найденное смещение только при
  надёжном обнаружении и отклонении больше мёртвой зоны (ONSET_DEADBAND_S):
  внутри мёртвой зоны окно анализа (100 мс) полностью остаётся внутри рабочего
  участка (края отстоят на fade+guard ≈ 1.25 с), поэтому сдвиг сетки не даёт
  ничего, кроме изменения содержимого окон, и метрики обязаны остаться прежними.
"""
from __future__ import annotations

import math

import numpy as np

XRUN_MESSAGES_MAX = 20        # исходных строк XRUN сохраняем в JSON (сверх — только счётчик)
LENGTH_TOL_MS = 50.0          # нормальный допуск длины записи (кратко-/длиннее — статус)
ONSET_SCAN_S = 1.0            # предел поиска смещения онсета, с (±)
ONSET_DEADBAND_S = 0.05       # мёртвая зона: меньше — диапазоны не сдвигаются
ONSET_MIN_SNR_DB = 26.0       # подъём тона над фоном, ниже которого onset ненадёжен
ONSET_MAX_RESIDUAL = 0.15     # СКО остатка фита огибающей / амплитуда тона
ONSET_MAX_MISMATCH_S = 0.1    # рассогласование onset L и R, обесценивающее обнаружение


def scan_xrun_stderr(text: str) -> dict:
    """Подсчёт XRUN-сообщений в stderr одного процесса.

    Возвращает счётчики underrun/overrun, сохранённые строки (не больше
    XRUN_MESSAGES_MAX), объём stderr и признак усечения списка сообщений.
    """
    lines = text.splitlines()
    underrun = overrun = 0
    messages = []
    for ln in lines:
        low = ln.lower()
        if "underrun" in low or "overrun" in low:
            if "underrun" in low:
                underrun += 1
            if "overrun" in low:
                overrun += 1
            if len(messages) < XRUN_MESSAGES_MAX:
                messages.append(ln.strip()[:200])
    return {"count": underrun + overrun, "underrun_count": underrun,
            "overrun_count": overrun, "messages": messages,
            "messages_truncated": bool(underrun + overrun > len(messages)),
            "stderr_bytes": len(text.encode("utf-8", errors="replace"))}


def xrun_summary(playback_text: str, capture_text: str) -> dict:
    """Сводка XRUN по обоим процессам: воспроизведение и запись раздельно."""
    play = scan_xrun_stderr(playback_text)
    cap = scan_xrun_stderr(capture_text)
    return {"playback": play, "capture": cap,
            "total_xruns": play["count"] + cap["count"],
            "note": "отсутствие XRUN-сообщений не доказывает отсутствие аппаратных сбоев; "
                    "контролируется только stderr aplay/arecord, длина записи и огибающая онсета"}


def length_status(actual_frames: int, expected_frames: int, fs: int) -> dict:
    """Классификация длины записи: ok / short / long с отклонением в кадрах и мс."""
    dev_f = int(actual_frames) - int(expected_frames)
    dev_ms = dev_f / fs * 1000.0
    tol_f = LENGTH_TOL_MS / 1000.0 * fs
    if abs(dev_f) <= tol_f:
        status = "ok"
    elif dev_f < 0:
        status = "short"
    else:
        status = "long"
    return {"expected_frames": int(expected_frames), "actual_frames": int(actual_frames),
            "deviation_frames": dev_f, "deviation_ms": dev_ms,
            "tolerance_ms": LENGTH_TOL_MS, "status": status}


def _tone_amp_curve(x, fs: float, f0: float, t_lo: float, t_hi: float,
                    win_s: float = 0.05, hop_s: float = 0.025):
    """Когерентная амплитудная кривая тона (линейная, пик) по окнам win_s с шагом hop_s."""
    n = max(16, int(round(win_s * fs)))
    h = max(1, int(round(hop_s * fs)))
    a0 = max(0, int(round(t_lo * fs)))
    a1 = int(round(t_hi * fs))
    t = np.arange(n) / fs
    s = np.sin(2.0 * math.pi * f0 * t)
    c = np.cos(2.0 * math.pi * f0 * t)
    ss = float(s @ s); cc = float(c @ c); sc = float(s @ c)
    det = ss * cc - sc * sc
    ts, amps = [], []
    if det <= 1e-20:
        return np.array([]), np.array([])
    for off in range(a0, max(a0, a1 - n + 1), h):
        chunk = np.asarray(x[off:off + n], dtype=np.float64)
        if chunk.size != n:
            break
        ys = float(chunk @ s); yc = float(chunk @ c)
        a = (ys * cc - yc * sc) / det
        b = (yc * ss - ys * sc) / det
        ts.append((off + n / 2.0) / fs)
        amps.append(math.hypot(a, b))
    return np.array(ts), np.array(amps)


def _ramp_mean(t_a, t_b, r0: float, fade: float):
    """Среднее clip((t-r0)/fade, 0, 1) на отрезке [t_a, t_b] (замкнутая форма).

    Первообразная интеграла clip((t-r0)/fade,0,1): 0 при t<=r0,
    (t-r0)^2/(2*fade) внутри рампы, (t-r0)-fade/2 после неё.
    """
    a = np.asarray(t_a, dtype=np.float64)
    b = np.asarray(t_b, dtype=np.float64)

    def F(t):
        t = np.asarray(t, dtype=np.float64)
        return np.where(t <= r0, 0.0,
                        np.where(t >= r0 + fade, t - r0 - fade / 2.0,
                                 (t - r0) ** 2 / (2.0 * fade)))

    return (F(b) - F(a)) / np.maximum(b - a, 1e-12)


def _detect_channel(x, fs: float, f0: float, t0: float, fade: float) -> dict:
    """Поиск смещения онсета одного канала относительно ожидаемого t0 (начало fade-in).

    Фон оценивается ДО самой ранней возможной рампы (t0 - ONSET_SCAN_S), опорная
    амплитуда полного уровня — ПОСЛЕ самой поздней (t0 + ONSET_SCAN_S + fade),
    иначе при раннем/позднем старте SNR загрязняется самим искомым сигналом.
    """
    dur = x.shape[0] / fs
    out = {"expected_onset_s": t0, "deviation_s": None, "tau_best": None,
           "snr_db": None, "residual_fraction": None, "reliable": False,
           "search_boundary_hit": False, "reason": None}
    if t0 + ONSET_SCAN_S + fade + 1.0 > dur - 0.05:
        out["reason"] = ("запись короче окна онсета (нет опорного участка полного "
                         "уровня после самого позднего fade-in)")
        return out
    lo = max(0.0, t0 - ONSET_SCAN_S - 1.2)
    hi = t0 + ONSET_SCAN_S + fade + 1.2
    ts, amps = _tone_amp_curve(x, fs, f0, lo, hi)
    if ts.size < 20:
        out["reason"] = "слишком мало окон для оценки онсета"
        return out
    pre_m = (ts >= max(lo, t0 - ONSET_SCAN_S - 1.1)) & (ts <= t0 - ONSET_SCAN_S - 0.05)
    post_m = (ts >= t0 + ONSET_SCAN_S + fade + 0.05) & (ts <= t0 + ONSET_SCAN_S + fade + 1.0)
    pre = float(np.median(amps[pre_m])) if pre_m.any() else float(np.min(amps))
    post = float(np.median(amps[post_m])) if post_m.any() else float(np.max(amps))
    snr_db = 20.0 * math.log10(max(post, 1e-12) / max(pre, 1e-12))
    out["snr_db"] = snr_db
    if snr_db < ONSET_MIN_SNR_DB:
        out["reason"] = (f"тон не поднимается над фоном надёжно "
                         f"(SNR {snr_db:.1f} dB < {ONSET_MIN_SNR_DB:.0f} dB)")
        return out
    amp_ref = post
    ta = ts - 0.025      # начало окна = центр минус win/2 (win 50 мс)
    tb = ts + 0.025
    grid = np.arange(-ONSET_SCAN_S, ONSET_SCAN_S + 1e-9, 0.002)
    best = None
    for tau in grid:
        m = amp_ref * _ramp_mean(ta, tb, t0 + float(tau), fade)
        sse = float(np.sum((amps - m) ** 2))
        if best is None or sse < best[1]:
            best = (float(tau), sse)
    tau, sse = best
    # минимум на краю диапазона поиска = признак выхода за пределы достоверности
    i = int(np.argmin(np.abs(grid - tau)))
    boundary_hit = (i == 0 or i == len(grid) - 1)
    out["search_boundary_hit"] = bool(boundary_hit)
    # параболическое уточнение только во внутренних узлах
    if 0 < i < len(grid) - 1:
        def sse_at(tv):
            m = amp_ref * _ramp_mean(ta, tb, t0 + float(tv), fade)
            return float(np.sum((amps - m) ** 2))
        y0, y1, y2 = sse_at(grid[i - 1]), sse_at(grid[i]), sse_at(grid[i + 1])
        den = (y0 - 2 * y1 + y2)
        if den > 0:
            tau = tau + 0.5 * (y0 - y2) / den * 0.002
    model = amp_ref * _ramp_mean(ta, tb, t0 + tau, fade)
    sse = float(np.sum((amps - model) ** 2))
    model_energy = float(np.sum(model ** 2))
    residual_fraction = math.sqrt(sse / max(model_energy, 1e-30))
    out.update({"deviation_s": float(tau), "tau_best": float(tau),
                "residual_fraction": residual_fraction,
                "reliable": bool(residual_fraction <= ONSET_MAX_RESIDUAL
                                 and not boundary_hit)})
    if not out["reliable"]:
        out["reason"] = ("минимум фита на границе диапазона поиска (смещение за ±"
                         f"{ONSET_SCAN_S:.0f} с)" if boundary_hit else
                         f"остаток фита огибающей {residual_fraction:.3f} "
                         f"> {ONSET_MAX_RESIDUAL}")
    return out


def detect_playback_onset(raw, fs: int, tone_hz: float, expected_l: float,
                          expected_r: float, fade_s: float) -> dict:
    """Фактическое начало воспроизведения по обоим каналам.

    Ожидаемые onset'ы — начала fade-in в capture-времени (lead + idle для L,
    lead + idle + fade + duration + fade + gap для R). Смещение оценивается
    подгонкой фазы известной трапеции огибающей, надёжность — по SNR тона над
    фоном, остатку фита и согласованности L/R. Применять смещение к диапазонам
    стоит только при reliable и |сдвиг| > ONSET_DEADBAND_S.
    """
    res_l = _detect_channel(raw[:, 0], fs, tone_hz, expected_l, fade_s)
    res_r = _detect_channel(raw[:, 1], fs, tone_hz, expected_r, fade_s)
    reliable = res_l["reliable"]
    reason = res_l.get("reason")
    # Диагностика (основной алгоритм не меняется): R не подтвердил onset,
    # хотя L надёжен — временная привязка подтверждена только по L.
    right_channel_unverified = bool(reliable and not res_r["reliable"])
    if reliable and res_r["reliable"] and \
            abs(res_l["tau_best"] - res_r["tau_best"]) > ONSET_MAX_MISMATCH_S:
        reliable = False
        reason = (f"рассогласование onset L/R: {res_l['tau_best']:+.3f} vs "
                  f"{res_r['tau_best']:+.3f} с")
    # ненадёжное обнаружение не должно выглядеть как измеренное:
    # measured_onset_s/deviation_s = None, ожидаемый момент хранится отдельно
    dev = res_l["deviation_s"] if reliable else None
    measured = (expected_l + dev) if reliable else None
    applied = bool(reliable and abs(dev) > ONSET_DEADBAND_S)
    return {"expected_onset_s": float(expected_l),
            "measured_onset_s": measured,
            "deviation_s": dev,
            "confidence": {"snr_db_L": res_l["snr_db"],
                           "residual_fraction_L": res_l["residual_fraction"],
                           "snr_db_R": res_r["snr_db"],
                           "residual_fraction_R": res_r["residual_fraction"],
                           "deviation_s_L": res_l["tau_best"],
                           "deviation_s_R": res_r["tau_best"],
                           "search_boundary_hit_L": res_l["search_boundary_hit"],
                           "search_boundary_hit_R": res_r["search_boundary_hit"]},
            "reliable": bool(reliable), "applied": applied,
            "right_channel_unverified": right_channel_unverified,
            "offset_s": (float(dev) if applied else 0.0), "reason": reason}
