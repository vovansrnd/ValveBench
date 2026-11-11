"""Живые метрики и контроль целостности потока (0.1.20). Только numpy/scipy, без GUI и звука.

Зачем модуль
------------
* ``analyze_live_tone`` — THD / THD+N / N / SINAD / ENOB / DR и список гармоник по одному
  окну живого спектра (для панели «как в REW»). Уровни берутся по мощности лепестка окна
  Блэкмана–Харриса, а не по высоте одного бина, поэтому не зависят от расстройки тона
  относительно сетки FFT (потеря на «бочке» BH-окна до ~0.83 дБ при пиковом отсчёте).
  Гармоники ниже локального шумового пола помечаются как оценка сверху (``credible=False``)
  — та же семантика, что в ``dsp.analyze_tone``.
* ``detect_carrier_dropouts`` — провалы несущей на ведомом канале: быстрая демодуляция через
  накопленные суммы (без БПФ), длительность, глубина и фазовый скачок.
* ``detect_sample_glitches`` — изолированные аномалии шириной в 1–2 отсчёта (физически не
  могут быть аналоговыми: импульсная характеристика децимирующего FIR АЦП длится десятки
  отсчётов) и широкие всплески. Источник НЕ называется: USB, FIFO, I2S, DMA и буфер
  драйвера дают одинаковую картину.
* ``hf_burst_track`` — огибающая сверхзвуковой «шумовой полки» по блокам 10 мс и выбросы.
* ``frame_integrity`` — всё вместе для одного окна: годится ли оно для усреднения/показа.

Принцип: данные с событием не усредняются и не выдаются за результат; событие всегда
показывается пользователю.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import signal as sg

from . import dsp

# Пороги — начальные, подлежат пересмотру по реальным записям.
DROPOUT_DEPTH_DB = 3.0        # провал огибающей несущей относительно медианы
DROPOUT_MIN_S = 0.002         # короче — не считаем провалом (разрешение ≈ окно демодулятора)
GLITCH_SIGMA = 9.0            # порог выброса в робастных σ (MAD) второй разности остатка
GLITCH_MIN_ABS = 3e-7         # абсолютный пол порога (≈ −127 dBFS), чтобы не ловить квантование
TONE_PRESENT_DB = 30.0        # тон «есть», если выше шумового пола спектра не менее чем на столько
MARGIN_OK_DB = dsp.MARGIN_OK_DB


# ------------------------------------------------------------------ вспомогательное


def _movmean_complex(z: np.ndarray, n: int) -> np.ndarray:
    """Центрированное скользящее среднее через накопленные суммы (O(N), без БПФ).

    На краях окно усекается (деление на фактическое число отсчётов)."""
    n = max(1, int(n))
    c = np.concatenate(([0j], np.cumsum(z)))
    idx = np.arange(len(z))
    lo = np.clip(idx - n // 2, 0, len(z))
    hi = np.clip(idx - n // 2 + n, 0, len(z))
    return (c[hi] - c[lo]) / np.maximum(hi - lo, 1)


class _Demod:
    """Однократная демодуляция канала на f0; из неё выводятся огибающая, фаза, остаток."""

    def __init__(self, x, fs, f0, n_periods=None):
        self.x = np.asarray(x, dtype=np.float64)
        self.fs, self.f0 = float(fs), float(f0)
        if n_periods is None:
            n_periods = max(3, int(math.ceil(0.002 * f0)))
        self.win = max(8, int(round(n_periods * fs / f0)))
        n = np.arange(self.x.size, dtype=np.float64)
        self._rot = np.exp(2j * np.pi * f0 * n / fs)
        self.zm = _movmean_complex(self.x * np.conj(self._rot), self.win)

    def residual(self):
        """x минус реконструированная несущая."""
        return self.x - 2.0 * np.real(self.zm * self._rot)


def refine_tone(x, fs, f_est, iters=4):
    """Уточнить частоту тона по фазовому набегу между половинами окна и вычесть синус МНК.

    Возвращает (f_ref, amp_peak, residual). Точность частоты ограничена шумом, а не сеткой
    БПФ: остаток после вычитания не содержит утечки окна, так что N и THD+N не упираются в
    боковые лепестки Блэкмана–Харриса (≈ −92 дБ относительно тона)."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    idx = np.arange(n, dtype=np.float64)
    h = n // 2
    f = float(f_est)
    for _ in range(max(1, int(iters))):
        rot = np.exp(-2j * np.pi * f * idx / fs)
        ca = np.sum(x[:h] * rot[:h])
        cb = np.sum(x[h:2 * h] * rot[h:2 * h])
        dphi = float(np.angle(cb * np.conj(ca)))
        # cb относительно ca уже набрал 2π·f·h/fs от опорного вращения; остаток — расстройка
        f += dphi * fs / (2.0 * np.pi * h)
    rot = np.exp(-2j * np.pi * f * idx / fs)
    c = 2.0 * np.mean(x * rot)
    fit = np.real(c * np.conj(rot))
    return f, float(np.abs(c)), x - fit


def _runs(mask: np.ndarray):
    """[(начало, конец_включительно)] непрерывных участков True."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def _merge(runs, gap):
    out = []
    for a, b in runs:
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


# ------------------------------------------------------------------ живой анализ тона


def analyze_live_tone(x, fs, f_hint=None, nharm=9, band=(20.0, 20000.0), mains=50.0,
                      half=dsp.HALF):
    """Метрики тона в одном окне (канал, 1-D). Возвращает dict; при отсутствии тона ok=False.

    THD — H2…H{nharm} (в пределах band и 0.45·Fs). Уровень каждой гармоники — мощность
    лепестка ±half бинов минус ожидаемый шум лепестка (по соседним бинам без линий).
    """
    x = np.asarray(x, dtype=np.float64)
    blk0 = dsp.Block(x, fs)               # спектр исходного окна — только для поиска тона
    P0, df, n = blk0.P, blk0.df, blk0.n
    nb = P0.size
    out = {"ok": False, "reason": "", "fs": float(fs), "n": int(n), "df": float(df)}

    b_lo = float(band[0])
    b_hi = min(float(band[1]), 0.45 * float(fs))
    ka = max(1, int(math.ceil(b_lo / df)))
    kb = min(nb - 1, int(b_hi / df))
    if kb <= ka + 20:
        out["reason"] = "полоса анализа слишком узкая для этого FFT"
        return out

    # --- поиск основного тона
    if f_hint:
        w = max(8, int(0.01 * f_hint / df))
        lo, hi = max(ka, int(f_hint / df) - w), min(kb, int(f_hint / df) + w + 1)
    else:
        lo, hi = ka, kb
    if hi <= lo + 2:
        out["reason"] = "ожидаемая частота вне полосы анализа"
        return out
    k0 = lo + int(np.argmax(P0[lo:hi]))
    # Пик должен заметно возвышаться над медианой полосы, иначе тона нет.
    floor_med = float(np.median(P0[ka:kb]))
    if P0[k0] < floor_med * 10 ** (TONE_PRESENT_DB / 10.0):
        out["reason"] = "основной тон не обнаружен (нет линии выше шумового пола)"
        return out
    f_c = blk0.centroid(k0 * df)
    # Уточнить частоту, вычесть синус МНК и дальше работать со спектром остатка:
    # гармоники, шум и THD+N не искажаются утечкой окна от основного тона.
    f_est, amp_peak, resid = refine_tone(x, fs, f_c)
    if abs(f_est - f_c) > 3 * df:           # уточнение ушло от пика — не доверяем
        f_est, amp_peak = f_c, math.sqrt(2.0 * float(np.sum(P0[max(0, k0 - 5): k0 + 6])))
        resid = x
    blk = dsp.Block(resid, fs)
    P = blk.P
    half_f = min(half, max(1, (k0 - 1) // 2))

    def lobe(k):
        return float(np.sum(P[max(0, k - half_f): k + half_f + 1]))

    Pf = amp_peak * amp_peak / 2.0         # мощность тона = A²/2, независимо от окна
    if Pf <= 0:
        out["reason"] = "нулевая мощность тона"
        return out

    # --- маска линий для оценки шума
    skip = np.zeros(nb, dtype=bool)
    skip[max(0, k0 - half_f - 2): k0 + half_f + 3] = True
    harm_k = []
    for h in range(2, int(nharm) + 1):
        fh = h * f_est
        if fh > b_hi:
            break
        kh = int(round(fh / df))
        if kh >= nb - half_f - 1:
            break
        harm_k.append((h, fh, kh))
        skip[max(0, kh - half_f - 2): kh + half_f + 3] = True
    mains_k = []
    if mains and mains > 0:
        for m in range(1, 7):
            km = int(round(m * mains / df))
            if 0 < km < nb:
                mains_k.append(km)

    def local_noise_per_bin(k, span=60):
        a, b = max(1, k - span), min(nb, k + span + 1)
        seg = P[a:b][~skip[a:b]]
        if seg.size < 8:
            seg = P[max(1, k - 4 * span): min(nb, k + 4 * span + 1)]
        # Среднее экспоненциально распределённых бинов = медиана / ln 2 — устойчиво к выбросам
        return float(np.median(seg) / math.log(2.0))

    lobe_bins = 2 * half_f + 1
    harms = []
    det_power = 0.0
    raw_power = 0.0
    for h, fh, kh in harm_k:
        p_obs = lobe(kh)
        p_noise = local_noise_per_bin(kh) * lobe_bins
        margin = dsp.db10(p_obs / max(p_noise, 1e-300)) if p_obs > 0 else -300.0
        credible = bool(margin >= MARGIN_OK_DB)
        p_corr = max(p_obs - p_noise, 0.0)
        p_used = p_corr if credible else p_obs
        raw_power += p_obs
        if credible:
            det_power += p_corr
        harms.append({
            "h": h, "f": fh, "k": kh, "credible": credible, "margin_db": margin,
            "dbc": dsp.db10(p_used / Pf) if p_used > 0 else -300.0,
            "pct": 100.0 * math.sqrt(p_used / Pf),
            "dbfs": dsp.rms_dbfs(math.sqrt(p_used)) if p_used > 0 else -300.0,
            "floor_dbc": dsp.db10(p_noise / Pf) if p_noise > 0 else -300.0,
        })
    thd = math.sqrt(det_power / Pf)
    thd_upper = math.sqrt(raw_power / Pf)
    n_cred = sum(1 for h in harms if h["credible"])

    # --- шум и THD+N в полосе: всё, кроме лепестка тона (и гармоник — для N)
    sel = np.zeros(nb, dtype=bool)
    sel[ka:kb + 1] = True
    fund = np.zeros(nb, dtype=bool)
    fund[max(0, k0 - half_f): k0 + half_f + 1] = True
    hmask = np.zeros(nb, dtype=bool)
    for _h, _f, kh in harm_k:
        hmask[max(0, kh - half_f): kh + half_f + 1] = True
    p_dn = float(np.sum(P[sel & ~fund]))
    p_n = float(np.sum(P[sel & ~fund & ~hmask]))
    fr = blk.freq
    aw = np.zeros(nb)
    m_sel = sel & ~fund & ~hmask
    aw[m_sel] = 10.0 ** (dsp.a_weight_db(fr[m_sel]) / 10.0)
    p_n_a = float(np.sum(P[m_sel] * aw[m_sel]))

    level_dbfs = dsp.rms_dbfs(math.sqrt(Pf))
    noise_dbfs = dsp.rms_dbfs(math.sqrt(max(p_n, 1e-300)))
    noise_a_dbfs = dsp.rms_dbfs(math.sqrt(max(p_n_a, 1e-300)))
    thdn_db = dsp.db10(p_dn / Pf)
    sinad = -thdn_db
    out.update({
        "ok": True, "f0": f_est, "k0": k0, "level_dbfs": level_dbfs,
        "harmonics": harms, "nharm": len(harms),
        "thd_pct": 100.0 * thd, "thd_db": dsp.db20(thd),
        "thd_upper_pct": 100.0 * thd_upper, "thd_upper_db": dsp.db20(thd_upper),
        "thd_bound": bool(harms) and n_cred == 0,
        "thd_partial": bool(harms) and 0 < n_cred < len(harms),
        "thdn_pct": 100.0 * math.sqrt(p_dn / Pf), "thdn_db": thdn_db,
        "noise_dbfs": noise_dbfs, "noise_a_dbfs": noise_a_dbfs,
        "snr_db": level_dbfs - noise_dbfs,
        "sinad_db": sinad, "enob": (sinad - 1.76) / 6.02,
        # DR здесь — оценка «полная шкала минус шум в полосе» (не AES17 с тоном −60 dBFS)
        "dr_db": -noise_dbfs, "dr_a_db": -noise_a_dbfs,
        "band": (b_lo, b_hi), "mains_k": mains_k,
    })
    return out


# ------------------------------------------------------------------ провалы несущей


def carrier_track(x, fs, f0, step_s=0.002, demod=None):
    """Огибающая несущей по шагам step_s: (t, env, win_samples).

    env — пиковая амплитуда несущей (0 dBFS = 1.0). ``demod`` — готовый _Demod канала."""
    d = demod or _Demod(x, fs, f0)
    step = max(1, int(round(step_s * fs)))
    idx = np.arange(step // 2, len(d.zm), step)
    return idx / float(fs), 2.0 * np.abs(d.zm[idx]), d.win


def tone_present(x, fs, f0, min_dbfs=-70.0, demod=None):
    """Есть ли на канале заведомо ведомая несущая f0 (медиана огибающей выше min_dbfs)."""
    _t, env, _w = carrier_track(x, fs, f0, step_s=0.01, demod=demod)
    return bool(env.size) and dsp.db20(float(np.median(env))) >= min_dbfs


def detect_carrier_dropouts(x, fs, f0, depth_db=DROPOUT_DEPTH_DB, min_s=DROPOUT_MIN_S,
                            step_s=0.001, demod=None):
    """Провалы амплитуды несущей: [{'start_s','dur_s','depth_db'}].

    Применять ТОЛЬКО к каналу, в который реально подан тон (см. tone_present): на тихом
    канале шум порождал бы ложные «остановки несущей». Потеря отсчётов БЕЗ обнуления
    амплитуду не меняет — её ловит detect_phase_jumps."""
    t, env, _win = carrier_track(x, fs, f0, step_s=step_s, demod=demod)
    if env.size < 8:
        return []
    ref = float(np.median(env))
    if ref <= 0:
        return []
    db = np.maximum(20.0 * np.log10(np.maximum(env, 1e-12) / ref), -120.0)
    runs = _merge(_runs(db < -abs(depth_db)), gap=2)
    ev = []
    for a, b in runs:
        dur = (b - a + 1) * step_s
        if dur < min_s:
            continue
        ev.append({"start_s": float(t[a]), "dur_s": float(dur),
                   "depth_db": float(np.min(db[a:b + 1])),
                   "at_edge": bool(a == 0 or b == env.size - 1)})
    return ev


def detect_phase_jumps(x, fs, f0, thr_deg=15.0, demod=None):
    """Скачки фазы несущей: [{'t_s','jump_deg'}] — признак потери/вставки отсчётов или паузы.

    Скачок на целое число периодов (D·f0 ≈ целое) принципиально невидим. Знак скачка
    НЕ используется для различения «пауза потока» и «потеря отсчётов»: для этого длительность
    нужна с точностью много лучше периода тона (1 мс = 1 период на 1 кГц), а края события
    по огибающей так точно не определяются — поэтому направление всегда 'undetermined'."""
    d = demod or _Demod(x, fs, f0)
    zm, win = d.zm, d.win
    lag = 2 * win
    if zm.size < 4 * lag:
        return []
    a, b = zm[lag:], zm[:-lag]
    mag = np.abs(zm)
    ref = float(np.median(mag))
    if ref <= 0:
        return []
    ok = (np.abs(a) > 0.5 * ref) & (np.abs(b) > 0.5 * ref)
    ph = np.angle(a * np.conj(b))
    base = float(np.median(ph[ok])) if ok.any() else 0.0
    j = (ph - base + np.pi) % (2 * np.pi) - np.pi
    j[~ok] = 0.0
    # Края окна: усечённое скользящее среднее даёт ложную фазу — отступаем на win
    j[:win] = 0.0
    j[-win:] = 0.0
    runs = _merge(_runs(np.abs(j) > math.radians(thr_deg)), gap=lag)
    out = []
    for a0, b0 in runs:
        k = a0 + int(np.argmax(np.abs(j[a0:b0 + 1])))
        out.append({"t_s": float((k + lag / 2.0) / fs), "jump_deg": float(math.degrees(j[k])),
                    "direction": "undetermined"})
    return out


# ------------------------------------------------------------------ аномалии отсчётов


def detect_sample_glitches(x, fs, f0=None, sigma=GLITCH_SIGMA, min_abs=GLITCH_MIN_ABS,
                           demod=None):
    """Резкие выбросы шириной до 19 отсчётов: [{'index','t_s','width','amp','kind'}].

    f0=None — канал без тона (тишина/шум): анализируется сам сигнал. С f0 несущая
    вычитается. Порог — робастный (MAD) по второй разности остатка: она убирает
    НЧ-составляющие (гармоники, дрейф), оставляя резкие события.
    kind: 'single_sample' (ширина ≤ 2 — аналоговой природы быть не может: импульсная
          характеристика децимирующего FIR АЦП длится десятки отсчётов),
          'short' (3–19). Источник (USB, FIFO, I2S, DMA, буфер драйвера) по картине
          не определяется. Край окна в win отсчётов исключён (усечённая демодуляция)."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < 256:
        return []
    if f0:
        d = demod or _Demod(x, fs, f0)
        r, guard = d.residual(), d.win
    else:
        r, guard = x - np.mean(x), 4
    h = np.abs(np.diff(r, 2))
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med))) * 1.4826
    thr = max(sigma * mad, 2.0 * min_abs)
    over = h > thr
    over[:guard] = False
    over[-guard:] = False
    ev = []
    for a, b in _merge(_runs(over), gap=3):
        # выброс шириной w даёт в d² серию длиной w+2
        w = max(1, (b - a + 1) - 2)
        if w >= 20:
            continue                        # широкие события — в detect_residual_bursts
        i0, i1 = a + 1, b + 1
        amp = float(np.max(np.abs(r[i0:i1 + 1])))
        ev.append({"index": int(i0), "t_s": float(i0 / fs), "width": int(w), "amp": amp,
                   "amp_dbfs": dsp.db20(amp), "kind": "single_sample" if w <= 2 else "short"})
    return ev


def detect_residual_bursts(x, fs, f0=None, step_s=0.001, rise_db=10.0, demod=None):
    """Широкие всплески остатка (≥ ~1 мс): [{'start_s','dur_s','rise_db'}].

    Блочный RMS остатка (после вычитания несущей) против робастной медианы. Ширина
    ≥ 1 мс — аналог (лампа/питание/наводка) или сбой передачи; по ширине не различить."""
    x = np.asarray(x, dtype=np.float64)
    if f0:
        d = demod or _Demod(x, fs, f0)
        r = d.residual()
        guard = d.win
    else:
        r, guard = x - np.mean(x), 4
    step = max(1, int(round(step_s * fs)))
    m = r.size // step
    if m < 16:
        return []
    p = np.mean(r[: m * step].reshape(m, step) ** 2, axis=1)
    db = 10.0 * np.log10(np.maximum(p, 1e-30))
    g = int(math.ceil(guard / step)) + 1
    med = float(np.median(db[g:-g]))
    mad = float(np.median(np.abs(db[g:-g] - med))) * 1.4826
    thr = med + max(rise_db, 5.0 * mad)
    over = db > thr
    over[:g] = False
    over[-g:] = False
    return [{"start_s": float(a * step_s), "dur_s": float((b - a + 1) * step_s),
             "rise_db": float(np.max(db[a:b + 1]) - med)} for a, b in _merge(_runs(over), gap=2)]


def detect_zero_runs(x, min_len=16):
    """Серии из min_len+ подряд идущих нулевых кадров (по обоим каналам одновременно)."""
    x = np.asarray(x)
    z = np.all(x == 0.0, axis=1) if x.ndim == 2 else (x == 0.0)
    return [{"index": a, "len": b - a + 1} for a, b in _runs(z) if b - a + 1 >= min_len]


# ------------------------------------------------------------------ ВЧ «шумовая полка»


def hf_burst_track(x, fs, f_lo=40000.0, step_s=0.01):
    """Уровень шума выше f_lo по блокам: (t, dbfs_rms). Для fs ≤ 2·f_lo возвращает пустое."""
    nyq = fs / 2.0
    if f_lo >= 0.9 * nyq:
        return np.zeros(0), np.zeros(0)
    sos = sg.butter(6, f_lo / nyq, btype="highpass", output="sos")
    y = sg.sosfilt(sos, np.asarray(x, dtype=np.float64))
    step = max(1, int(round(step_s * fs)))
    m = y.size // step
    if m < 8:
        return np.zeros(0), np.zeros(0)
    y = y[: m * step].reshape(m, step)[1:]          # первый блок — переходный процесс фильтра
    p = np.mean(y ** 2, axis=1)
    return (np.arange(1, m) + 0.5) * step / fs, dsp.rms_dbfs(np.sqrt(np.maximum(p, 1e-30)))


def detect_hf_bursts(t, db, rise_db=6.0, min_blocks=1):
    """Всплески ВЧ-уровня выше робастной медианы: [{'start_s','dur_s','rise_db'}]."""
    if db.size < 8:
        return []
    med = float(np.median(db))
    mad = float(np.median(np.abs(db - med))) * 1.4826
    thr = med + max(rise_db, 4.0 * mad)
    step = float(t[1] - t[0])
    return [{"start_s": float(t[a] - step / 2), "dur_s": (b - a + 1) * step,
             "rise_db": float(np.max(db[a:b + 1]) - med)}
            for a, b in _runs(db > thr) if b - a + 1 >= min_blocks]


# ------------------------------------------------------------------ сводка по окну


@dataclass
class ChannelIntegrity:
    name: str
    driven: bool = False
    dropouts: list = field(default_factory=list)
    phase_jumps: list = field(default_factory=list)
    glitches: list = field(default_factory=list)
    bursts: list = field(default_factory=list)
    hf_bursts: list = field(default_factory=list)
    note: str = ""

    @property
    def fatal(self):
        """Событие, из-за которого окно нельзя усреднять и показывать как результат.

        HF-всплески — информационные: окно не бракуют, но показывают."""
        return bool(self.dropouts or self.phase_jumps or self.glitches or self.bursts)


def frame_integrity(x, fs, f0=None, driven=(False, False), hf_lo=40000.0, names=("L", "R")):
    """Проверка одного окна (n, 2). driven[c] — подан ли в канал c тон f0.

    Провалы/скачки фазы несущей — только на ведомых каналах (на тихом шум давал бы ложные
    «остановки несущей»). Аномалии отсчётов и всплески — на всех (на ведомых с вычитанием
    несущей). ВЧ-всплески — информационные."""
    x = np.asarray(x, dtype=np.float64)
    res = []
    for c in range(x.shape[1]):
        ci = ChannelIntegrity(names[c] if c < len(names) else str(c))
        seg = x[:, c]
        dm = _Demod(seg, fs, f0) if (driven[c] and f0) else None
        is_driven = bool(dm is not None and tone_present(seg, fs, f0, demod=dm))
        ci.driven = is_driven
        if driven[c] and f0 and not is_driven:
            ci.note = "несущей на канале нет (проверка провалов пропущена)"
        if is_driven:
            ci.dropouts = detect_carrier_dropouts(seg, fs, f0, demod=dm)
            ci.phase_jumps = detect_phase_jumps(seg, fs, f0, demod=dm)
            if ci.phase_jumps:
                # Скачок фазы около 180° гасит среднее по окну демодулятора и выглядит как
                # короткий «провал» амплитуды. Короткий провал, внутри которого найден скачок
                # фазы, — это скачок фазы, а не пауза несущей.
                ws = dm.win / float(fs)
                ci.dropouts = [d for d in ci.dropouts
                               if not (d["dur_s"] <= 3.0 * ws + 0.002 and any(
                                   d["start_s"] - ws <= j["t_s"] <= d["start_s"] + d["dur_s"] + ws
                                   for j in ci.phase_jumps))]
        f_use = f0 if is_driven else None
        ci.glitches = detect_sample_glitches(seg, fs, f_use, demod=dm if is_driven else None)
        ci.bursts = detect_residual_bursts(seg, fs, f_use, demod=dm if is_driven else None)
        # Края провала несущей сами дают широкополосный остаток, а шумовой всплеск — серию
        # «выбросов»: это одно событие, а не три. Вложенные события не дублируем.
        pad = (dm.win / float(fs) if dm is not None else 0.0) + 0.004
        spans = [(d["start_s"] - pad, d["start_s"] + d["dur_s"] + pad) for d in ci.dropouts]
        ci.bursts = [b for b in ci.bursts if not _overlaps(b["start_s"], b["start_s"] + b["dur_s"], spans)]
        spans += [(b["start_s"] - 0.001, b["start_s"] + b["dur_s"] + 0.001) for b in ci.bursts]
        ci.glitches = [g for g in ci.glitches if not _overlaps(g["t_s"], g["t_s"], spans)]
        t, db = hf_burst_track(seg, fs, hf_lo)
        ci.hf_bursts = detect_hf_bursts(t, db)
        res.append(ci)
    return res


def _overlaps(a, b, spans):
    return any(a <= hi and b >= lo for lo, hi in spans)


def describe(res):
    """Короткий текст для плашки: пустая строка, если всё чисто."""
    parts = []
    for ci in res:
        for d in ci.dropouts:
            parts.append(f"{ci.name}: провал несущей {d['dur_s'] * 1e3:.0f} мс")
        for j in ci.phase_jumps:
            parts.append(f"{ci.name}: скачок фазы {j['jump_deg']:+.0f}°")
        singles = [g for g in ci.glitches if g["kind"] == "single_sample"]
        if singles:
            parts.append(f"{ci.name}: аномалия 1 отсчёта ×{len(singles)}")
        shorts = [g for g in ci.glitches if g["kind"] != "single_sample"]
        if shorts:
            parts.append(f"{ci.name}: выброс {shorts[0]['width']} отсч.")
        for b in ci.bursts:
            parts.append(f"{ci.name}: всплеск {b['dur_s'] * 1e3:.0f} мс (+{b['rise_db']:.0f} дБ)")
    return "; ".join(parts)


def describe_info(res):
    """Информационные события (окно не бракуется)."""
    parts = []
    for ci in res:
        for b in ci.hf_bursts:
            parts.append(f"{ci.name}: ВЧ-всплеск {b['dur_s'] * 1e3:.0f} мс (+{b['rise_db']:.0f} дБ)")
    return "; ".join(parts)


# ------------------------------------------------------------------ события потока и состояние


def window_events(events, start, end, fatal_only=True):
    """События backend, чей подозрительный интервал [a, b] (в кадрах) пересекает окно
    [start, end]. При неизвестных границах (симулятор) — пусто."""
    if start is None or end is None:
        return []
    return [e for e in events
            if (e.get("fatal") or not fatal_only)
            and e["suspect"][1] >= start and e["suspect"][0] <= end]


def describe_event(e):
    dur = e.get("dur_ms")
    dtxt = f" ≥{dur:.0f} мс" if dur else ""
    if e["kind"] == "xrun_capture":
        return f"XRUN АЦП (потеря отсчётов){dtxt}"
    if e["kind"] == "xrun_playback":
        return f"XRUN ЦАП (разрыв тона){dtxt}"
    if e["kind"] == "capture_gap":
        return f"пауза прихода данных {e.get('gap_ms', 0):.0f} мс"
    return e["kind"]


_SEVERITY = {None: -1, "ok": 0, "info": 1, "bad": 2}


def merge_health(a, b):
    """Из двух (level, text) — более серьёзное."""
    return a if _SEVERITY.get(a[0], -1) >= _SEVERITY.get(b[0], -1) else b


def health_summary(problem, status, events, now, window_s=6.0):
    """(level, text) для плашки состояния потока. level: 'ok' | 'info' | 'bad'."""
    status = status or {}
    if problem:
        return "bad", f"Нет данных: {problem}"
    recent = [e for e in events if now - e["t"] <= window_s]
    fatal = [e for e in recent if e["fatal"]]
    if fatal:
        e = fatal[-1]
        more = f" (+{len(fatal) - 1})" if len(fatal) > 1 else ""
        return "bad", f"{describe_event(e)}{more} · {now - e['t']:.0f} с назад"
    info = [e for e in recent if not e["fatal"]]
    if info:
        e = info[-1]
        return "info", f"{describe_event(e)} · {now - e['t']:.0f} с назад"
    total = status.get("xruns") or 0
    if total:
        return "info", f"Поток OK · XRUN за сессию: {total}"
    return "ok", "Поток OK"


def octave_smooth(freq, p, per_octave=6):
    """Среднее мощности по долям октавы (по логарифмической оси): (центры, мощность).

    В отличие от dsp._frac_octave усредняет все бины в каждой полосе, а не интерполирует
    отдельные бины, поэтому на ВЧ, где в полосе сотни бинов, шум не алиасится."""
    freq = np.asarray(freq, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    ok = freq > 0
    freq, p = freq[ok], p[ok]
    lf = np.log2(freq)
    b = np.floor((lf - lf[0]) * per_octave).astype(int)
    cnt = np.bincount(b).astype(float)
    good = cnt > 0
    s = np.bincount(b, weights=p)
    fc = np.bincount(b, weights=freq)
    return fc[good] / cnt[good], s[good] / cnt[good]
