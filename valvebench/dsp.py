"""Измерительное ядро: только numpy/scipy, никакого звука и GUI.

Соглашения
----------
* 0 dBFS — синус с пиком 1.0. Уровень сигнала с эффективным значением r:
  20·log10(√2·r). Так «тон −6 dBFS» и «шум −110 dBFS» сравнимы напрямую.
* Все линейные измерения идут по когерентным окнам: частота тона кратна
  fs/n, поэтому любое окно из n отсчётов стационарного тона содержит целое
  число периодов — выравнивать запись относительно воспроизведения не нужно.
* Мощности считаются суммой бинов по «лепестку» окна Блэкмана-Харриса, а не
  по пику: результат не зависит от расстройки частоты и корректен для шума
  (ENBW учтён нормировкой на Σw²).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import signal as sg
from scipy.fft import next_fast_len

SQ2 = math.sqrt(2.0)
HALF = 5          # полуширина лепестка BH в бинах (главный лепесток ±4)
_TINY = 1e-18

# ----------------------------------------------------------------- утилиты


def db20(v):
    v = np.asarray(v, dtype=np.float64)
    out = 20.0 * np.log10(np.maximum(np.abs(v), _TINY))
    return float(out) if out.ndim == 0 else out


def db10(p):
    p = np.asarray(p, dtype=np.float64)
    out = 10.0 * np.log10(np.maximum(p, _TINY * _TINY))
    return float(out) if out.ndim == 0 else out


def rms_dbfs(rms):
    """Эффективное значение → dBFS (полный синус = 0)."""
    return db20(np.asarray(rms) * SQ2)


def dbfs_to_amp(dbfs):
    return 10.0 ** (np.asarray(dbfs, dtype=np.float64) / 20.0)


def coherent_freq(f, fs, n):
    """Ближайшая к f частота, кратная fs/n (целое число периодов в n отсчётах)."""
    k = max(1, int(round(f * n / fs)))
    return k * fs / n


_WIN: dict[int, np.ndarray] = {}


def bh(n):
    if n not in _WIN:
        _WIN[n] = sg.get_window("blackmanharris", n, fftbins=True)
    return _WIN[n]


# ------------------------------------------------------------- спектр блока


class Block:
    """Окно анализа: P[k] — мощность (rms²) бина, так что сумма по лепестку
    тона равна его rms²; amp[k] — пиковая амплитуда линии в бине (для графиков)."""

    def __init__(self, x, fs):
        x = np.asarray(x, dtype=np.float64)
        n = x.size
        w = bh(n)
        X = np.fft.rfft(x * w)
        self.fs, self.n = float(fs), n
        self.df = fs / n
        self.P = 2.0 * np.abs(X) ** 2 / (n * np.sum(w * w))
        self.P[0] *= 0.5
        if n % 2 == 0:
            self.P[-1] *= 0.5
        self.amp = 2.0 * np.abs(X) / np.sum(w)
        self.freq = np.arange(self.P.size) * self.df

    def k(self, f):
        return int(round(f / self.df))

    def lobe(self, f, half=HALF):
        k = self.k(f)
        return float(np.sum(self.P[max(0, k - half): k + half + 1]))

    def band(self, lo, hi):
        a, b = max(1, self.k(lo)), min(self.P.size, self.k(hi) + 1)
        return float(np.sum(self.P[a:b]))

    def centroid(self, f, half=3):
        k = self.k(f)
        a, b = max(0, k - half), min(self.P.size, k + half + 1)
        p = self.P[a:b]
        s = float(np.sum(p))
        return float(np.sum(self.freq[a:b] * p) / s) if s > 0 else f


def _blocks(x, n):
    m = len(x) // n
    if m < 1:
        raise ValueError(f"запись короче одного окна: {len(x)} < {n}")
    return np.asarray(x[: m * n], dtype=np.float64).reshape(m, n)


# --------------------------------------------------------------- один тон


def analyze_tone(x, fs, f0, n, nharm=9, band=(20.0, 20000.0), mains=50.0, half=HALF):
    """THD, THD+N, гармоники, фон. x — установившийся сигнал из M·n отсчётов.

    THD    — по синхронно усреднённому периоду: шум подавляется в √M раз, поэтому
             гармоники ниже шумового пола видны.
    THD+N  — по неусреднённым окнам (мощности усредняются): настоящий шум.
             Фундаментальный лепесток исключается маской, а не вычитанием
             мощностей — иначе ошибка оценки несущей забивает результат.
    """
    blocks = _blocks(x, n)
    M = blocks.shape[0]
    raw = [Block(b, fs) for b in blocks]
    Pdir = np.mean([b.P for b in raw], axis=0)
    ref = raw[0]
    df = ref.df
    warn = []

    k0 = ref.k(f0)
    lo, hi = max(1, k0 - 3), k0 + 4
    k0 = lo + int(np.argmax(Pdir[lo:hi]))
    f_est = Block.centroid(_Shim(ref, Pdir), k0 * df)
    coh_err = f_est / df - round(f_est / df)
    if abs(coh_err) > 0.05:
        warn.append(f"тон не когерентен окну (сдвиг {coh_err:+.2f} бина): частоты ЦАП и АЦП "
                    "расходятся, усреднение отключено")
        avg_block = ref
        M_eff = 1
    else:
        avg_block = Block(blocks.mean(axis=0), fs)
        M_eff = M

    half_f = min(half, max(1, (k0 - 1) // 2))
    Pf = float(np.sum(avg_block.P[max(0, k0 - half_f): k0 + half_f + 1]))
    if Pf <= 0:
        raise ValueError("тон не найден")

    nyq_lim = 0.45 * fs
    b_lo, b_hi = band[0], min(band[1], nyq_lim)
    harm = []
    masked = np.zeros(Pdir.size, dtype=bool)
    masked[max(0, k0 - half_f): k0 + half_f + 1] = True
    ph = 0.0
    for h in range(2, nharm + 1):
        fh = h * f_est
        if fh > b_hi:
            break
        kh = int(round(fh / df))
        a, b = max(0, kh - half_f), kh + half_f + 1
        p = float(np.sum(avg_block.P[a:b]))
        masked[a:b] = True
        ph += p
        harm.append({"h": h, "f": fh, "dbc": db10(p / Pf) / 1.0 if p > 0 else -300.0,
                     "pct": 100.0 * math.sqrt(p / Pf)})
    # dbc выше посчитан как 10log10(P/Pf) = 20log10(A/A0) — корректно
    thd = math.sqrt(ph / Pf)

    ka, kb = max(1, int(b_lo / df)), min(Pdir.size, int(b_hi / df) + 1)
    sel = np.zeros(Pdir.size, dtype=bool)
    sel[ka:kb] = True
    # THD+N = всё в полосе, кроме лепестка основного тона
    fund_mask = np.zeros(Pdir.size, dtype=bool)
    fund_mask[max(0, k0 - half_f): k0 + half_f + 1] = True
    p_dn = float(np.sum(Pdir[sel & ~fund_mask]))
    p_n = float(np.sum(Pdir[sel & ~masked]))
    Pf_dir = float(np.sum(Pdir[fund_mask]))

    hum = []
    if mains > 0:
        for m in range(1, 7):
            fm = m * mains
            if fm > b_hi or fm < b_lo:
                continue
            if any(abs(fm - h * f_est) < (half_f + 2) * df for h in range(1, nharm + 2)):
                continue
            p = float(np.sum(Pdir[max(0, int(round(fm / df)) - 3): int(round(fm / df)) + 4]))
            hum.append({"f": fm, "dbfs": rms_dbfs(math.sqrt(p)),
                        "dbc": db10(p / Pf_dir)})

    spec_amp = np.sqrt(np.mean([b.amp ** 2 for b in raw], axis=0))
    return {
        "f0": f_est, "n": n, "blocks": M, "coh_err_bins": coh_err,
        "level_dbfs": rms_dbfs(math.sqrt(Pf)), "rms": math.sqrt(Pf),
        "harmonics": harm,
        "thd_pct": 100.0 * thd, "thd_db": db20(thd),
        "thdn_pct": 100.0 * math.sqrt(p_dn / Pf_dir), "thdn_db": db10(p_dn / Pf_dir),
        "noise_dbc": db10(p_n / Pf_dir),            # шум+фон без гармоник
        "noise_dbfs": rms_dbfs(math.sqrt(p_n)),
        "hum": hum, "warnings": warn,
        "freq": ref.freq, "spec_db": db20(spec_amp),
    }


class _Shim:
    """Позволяет вызвать Block.centroid с усреднённой мощностью."""

    def __init__(self, blk, P):
        self.P, self.freq, self.df, self.k = P, blk.freq, blk.df, blk.k


# ------------------------------------------------------------- два тона (IMD)


def analyze_imd(x, fs, f1, f2, n, mode="ccif", band_hi=None, half=HALF):
    """IMD двухтонального сигнала. mode: 'ccif' (равные тона) или 'smpte' (4:1).

    Продукты считаются по усреднённому периоду (шум подавлен).
    CCIF  : d2 = f2−f1, d3 = 2f1−f2, 2f2−f1, d5; проценты — к (A1+A2) (IEC 60268-3).
    SMPTE : боковые fв±m·fн, m=1..4; проценты — к амплитуде верхнего тона.
    """
    blocks = _blocks(x, n)
    avg = Block(blocks.mean(axis=0), fs)
    lo, hi = sorted((f1, f2))
    amp = lambda f: math.sqrt(avg.lobe(f, half))          # rms линии
    a_lo, a_hi = amp(lo), amp(hi)
    lim = min(band_hi or 0.45 * fs, 0.45 * fs)
    prods = []
    if mode == "smpte":
        for m in range(1, 5):
            for s, sign in ((f"fв−{m}fн", -1), (f"fв+{m}fн", +1)):
                f = hi + sign * m * lo
                if 20 < f < lim:
                    prods.append({"label": s, "f": f, "order": m + 1, "rms": amp(f)})
        ref_rms = a_hi
        ref_name = "fв"
    else:
        cand = [("fв−fн", hi - lo, 2), ("2fн−fв", 2 * lo - hi, 3), ("2fв−fн", 2 * hi - lo, 3),
                ("3fн−2fв", 3 * lo - 2 * hi, 5), ("3fв−2fн", 3 * hi - 2 * lo, 5),
                ("fв+fн", hi + lo, 2)]
        for s, f, o in cand:
            if 20 < f < lim:
                prods.append({"label": s, "f": f, "order": o, "rms": amp(f)})
        ref_rms = (a_lo + a_hi) / 2.0           # (A1+A2)/√2·… см. ниже
        ref_name = "(A1+A2)"
    for p in prods:
        p["dbc"] = db20(p["rms"] / max(ref_rms, _TINY))
    tot = math.sqrt(sum(p["rms"] ** 2 for p in prods))
    d2 = math.sqrt(sum(p["rms"] ** 2 for p in prods if p["order"] % 2 == 0))
    d3 = math.sqrt(sum(p["rms"] ** 2 for p in prods if p["order"] % 2 == 1))
    if mode == "smpte":
        denom = a_hi
    else:
        denom = a_lo + a_hi                      # rms-суммы ≙ (A1+A2) в пиках
    return {
        "mode": mode, "a_lo_dbfs": rms_dbfs(a_lo), "a_hi_dbfs": rms_dbfs(a_hi),
        "products": prods, "ref": ref_name,
        "imd_pct": 100.0 * tot / denom, "imd_db": db20(tot / denom),
        "d2_pct": 100.0 * d2 / denom, "d3_pct": 100.0 * d3 / denom,
        "freq": avg.freq, "spec_db": db20(avg.amp),
    }


# -------------------------------------------------------------- шум


def a_weight_db(f):
    f = np.atleast_1d(np.asarray(f, dtype=np.float64))
    f2 = f * f
    ra = (12194.0 ** 2 * f2 * f2 /
          ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2)))
    return 20.0 * np.log10(np.maximum(ra, 1e-30)) + 2.0


def c_weight_db(f):
    f = np.atleast_1d(np.asarray(f, dtype=np.float64))
    f2 = f * f
    rc = 12194.0 ** 2 * f2 / ((f2 + 20.6 ** 2) * (f2 + 12194.0 ** 2))
    return 20.0 * np.log10(np.maximum(rc, 1e-30)) + 0.06


def analyze_noise(x, fs, band=(20.0, 20000.0), mains=50.0, nfft=None):
    """Шум: полоса, A/C-взвешивание, плотность, линии фона. Уровни — как у синуса
    (dBFS), плотность — dBFS/√Гц."""
    x = np.asarray(x, dtype=np.float64)
    x = x - np.mean(x)
    nfft = nfft or int(min(len(x), 2 ** int(math.log2(fs * 0.5))))   # ≈0.5 с
    f, pxx = sg.welch(x, fs=fs, window="blackmanharris", nperseg=nfft,
                      noverlap=int(nfft * 0.66), scaling="density", detrend=False)
    df = f[1] - f[0]
    hi = min(band[1], 0.45 * fs)
    m = (f >= band[0]) & (f <= hi)

    def total(w_db=None):
        p = pxx[m] if w_db is None else pxx[m] * 10 ** (w_db[m] / 10)
        return math.sqrt(float(np.sum(p) * df))

    hum = []
    for k in range(1, 7):
        fm = k * mains
        if mains <= 0 or not (band[0] <= fm <= hi):
            continue
        sel = (f > fm - 3 * df) & (f < fm + 3 * df)
        p = float(np.sum(pxx[sel]) * df)
        hum.append({"f": fm, "dbfs": rms_dbfs(math.sqrt(p))})
    return {
        "unw_dbfs": rms_dbfs(total()), "a_dbfs": rms_dbfs(total(a_weight_db(f))),
        "c_dbfs": rms_dbfs(total(c_weight_db(f))), "unw_rms": total(),
        "hum": hum, "freq": f, "dens_dbfs": db10(pxx / 0.5),   # dBFS/√Гц
        "df": df,
    }


# ------------------------------------------------------------- RIAA


_T1, _T2, _T3, _T4 = 3180e-6, 318e-6, 75e-6, 7950e-6


def _riaa_h(f, iec):
    w = 2j * np.pi * np.asarray(f, dtype=np.float64)
    h = (1 + w * _T2) / ((1 + w * _T1) * (1 + w * _T3))
    if iec:
        h = h * (w * _T4) / (1 + w * _T4)
    return h


def riaa_playback(f, iec=False):
    """Комплексная характеристика воспроизведения, 0 дБ на 1 кГц (по модулю)."""
    return _riaa_h(f, iec) / abs(_riaa_h(1000.0, iec))


def riaa_playback_db(f, iec=False):
    return db20(np.abs(riaa_playback(np.atleast_1d(f), iec)))


# ------------------------------------------------------- свип по Фарине


@dataclass
class SweepSpec:
    f1: float
    f2: float
    dur: float
    fs: float
    L: float = 0.0
    x: np.ndarray = field(default=None, repr=False)      # то, что играем (с предыскажением)
    x0: np.ndarray = field(default=None, repr=False)     # тот же свип без предыскажения
    gain: np.ndarray = field(default=None, repr=False)   # огибающая предыскажения g(t)


def make_sweep(f1, f2, dur, fs, preemph=None):
    """Экспоненциальный синус f1→f2 длительностью dur.

    preemph — функция f_Гц → амплитуда (линейно): предыскажение по мгновенной
    частоте (напр. inverse-RIAA, чтобы выход корректора был ровным).
    Деконволюция — точная, в частотной области, по свипу без предыскажения;
    АЧХ потом делится на g(f). (Аналитический «развёрнутый» фильтр Фарины для
    свипа со сглаженными краями даёт остаток −97 дБ на отсчёт, который окном
    в полсекунды превращается в −50 дБ ложных «гармоник».)
    """
    n = int(round(dur * fs))
    t = np.arange(n) / fs
    L = dur / math.log(f2 / f1)
    phi = 2 * math.pi * f1 * L * (np.exp(t / L) - 1.0)
    finst = f1 * np.exp(t / L)
    g = np.ones(n) if preemph is None else np.asarray(preemph(finst), dtype=np.float64)
    x = np.sin(phi)
    fi = min(0.08 * dur, 4.0 / f1)
    fo = 0.002
    nfi, nfo = int(fi * fs), int(fo * fs)
    env = np.ones(n)
    env[:nfi] = 0.5 - 0.5 * np.cos(np.pi * np.arange(nfi) / nfi)
    env[n - nfo:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(nfo) / nfo)
    x0 = x * env
    return SweepSpec(f1, f2, dur, fs, L, x0 * g, x0, g)



def _frac_octave(f, y_pow, frac):
    """Сглаживание мощности в 1/frac октавы (скользящее среднее по лог-оси)."""
    if frac <= 0:
        return y_pow
    lf = np.log2(np.maximum(f, 1e-9))
    grid = np.linspace(lf[0], lf[-1], 4000)
    yg = np.interp(grid, lf, y_pow)
    w = max(1, int(round(4000 / (lf[-1] - lf[0]) / frac)))
    ker = np.ones(w) / w
    ys = np.convolve(np.pad(yg, (w, w), mode="edge"), ker, mode="same")[w:-w]
    return np.interp(lf, grid, ys)


def analyze_sweep(y, spec, f_lo=20.0, f_hi=20000.0, nharm=5, thd_hi=20000.0,
                  smooth=24, thd_smooth=6, pts_per_oct=48):
    """Деконволюция записи y: АЧХ, фаза и THD по гармоникам как функции частоты."""
    fs = spec.fs
    y = np.asarray(y, dtype=np.float64)
    nx = len(spec.x0)
    nf0 = next_fast_len(len(y) + nx)
    X0 = np.fft.rfft(spec.x0, nf0)
    fr0 = np.fft.rfftfreq(nf0, 1 / fs)
    band = (fr0 > spec.f1 * 1.5) & (fr0 < spec.f2 * 0.8)
    eps = (0.003 * float(np.median(np.abs(X0[band])))) ** 2
    INV = np.conj(X0) / (np.abs(X0) ** 2 + eps)
    ir = np.fft.irfft(np.fft.rfft(y, nf0) * INV, nf0)
    ir = np.roll(ir, nx)                       # гармоники лежат ДО пика: уходят в «отрицательное» время
    p = int(np.argmax(np.abs(ir[nx: nx + len(y)]))) + nx
    L = spec.L
    c = {k: p - int(round(L * math.log(k) * fs)) for k in range(1, nharm + 2)}

    def seg(k):
        lo = (c[k] + c[k + 1]) // 2 if k + 1 in c else c[k] - int(0.5 * L * math.log(1 + 1 / k) * fs)
        if k == 1:
            hi = c[1] + (c[1] - c[2]) // 2
        else:
            hi = (c[k - 1] + c[k]) // 2
        return max(lo, 0), min(hi, len(ir))

    out_f = np.geomspace(f_lo, f_hi, int(math.log2(f_hi / f_lo) * pts_per_oct) + 1)

    def spectrum(k, ref):
        lo, hi = seg(k)
        m = hi - lo
        if m < 64:
            return None, None
        w = sg.windows.tukey(m, 0.25)
        nf = next_fast_len(max(m, int(fs / 2)))
        z = np.zeros(nf)
        idx = (np.arange(lo, hi) - ref) % nf
        z[idx] = ir[lo:hi] * w
        return np.fft.rfftfreq(nf, 1 / fs), np.fft.rfft(z)

    fq, H1 = spectrum(1, p)
    g_at = lambda f: np.interp(f, spec.f1 * np.exp(np.arange(len(spec.x)) / fs / L),
                               spec.gain)
    H1 = H1 / np.maximum(g_at(np.clip(fq, spec.f1, spec.f2)), 1e-12)
    mag1 = np.abs(H1)
    pow1 = np.interp(out_f, fq, mag1 ** 2)
    fr_db = db10(_frac_octave(out_f, pow1, smooth))
    phase = np.unwrap(np.angle(np.interp(out_f, fq, H1.real) + 1j * np.interp(out_f, fq, H1.imag)))

    # Гармоника k на ВЫХОДНОЙ частоте ν=k·f: |H_k(ν)| сравниваем с |H_1(f)|.
    # Оба — абсолютные амплитуды выхода при одной и той же развёртке, поэтому
    # предыскажение g(f) в отношении сокращается и делить на него не нужно.
    thd_f = out_f[out_f <= thd_hi]
    mag1_raw = np.abs(H1) * np.maximum(g_at(np.clip(fq, spec.f1, spec.f2)), 1e-12)
    pw1 = np.interp(thd_f, fq, mag1_raw ** 2)
    hp = np.zeros_like(thd_f)
    per_h = {}
    for k in range(2, nharm + 1):
        fk, Hk = spectrum(k, c[k])
        if Hk is None:
            continue
        valid = (thd_f * k) <= min(thd_hi, 0.45 * fs)
        pk = np.where(valid, np.interp(thd_f * k, fk, np.abs(Hk) ** 2), 0.0)
        per_h[k] = pk
        hp += pk
    den = np.maximum(_frac_octave(thd_f, pw1, thd_smooth), _TINY)
    thd_pow = _frac_octave(thd_f, hp, thd_smooth) / den
    no_h = (thd_f * 2) > min(thd_hi, 0.45 * fs)       # ни одной гармоники в полосе
    thd_pow[no_h] = np.nan
    return {
        "freq": out_f, "fr_db": fr_db, "phase": phase,
        "thd_freq": thd_f, "thd_db": db10(thd_pow), "thd_pct": 100.0 * np.sqrt(thd_pow),
        "h_db": {k: db10(_frac_octave(thd_f, v, thd_smooth) / den)
                 for k, v in per_h.items()},
        "delay_samples": p - nx,
    }


def analyze_transfer_noise(x, y, fs, band=(20.0, 20000.0), nperseg=None):
    """Оценка H1(f)=Sxy/Sxx и coherence для измерения АЧХ шумовым сигналом.

    SciPy csd(x,y) возвращает conj(X)*Y, поэтому Sxy/Sxx соответствует Y/X.
    Вне частот, где энергия тестового сигнала мала, результат не считается
    достоверным; coherence сохраняется отдельно.
    """
    x = np.asarray(x, dtype=np.float64).ravel()
    y = np.asarray(y, dtype=np.float64).ravel()
    n = min(x.size, y.size)
    if n < 8192:
        raise ValueError("для шумового измерения нужно не менее 8192 отсчётов")
    x, y = x[:n] - np.mean(x[:n]), y[:n] - np.mean(y[:n])
    if nperseg is None:
        target = max(8192, n // 4)
        nperseg = min(131072, 1 << int(np.floor(np.log2(target))))
    nperseg = int(min(nperseg, n))
    noverlap = int(nperseg * 0.5)
    f, pxx = sg.welch(x, fs=fs, window="hann", nperseg=nperseg, noverlap=noverlap,
                      detrend="constant", scaling="density")
    _, pyy = sg.welch(y, fs=fs, window="hann", nperseg=nperseg, noverlap=noverlap,
                      detrend="constant", scaling="density")
    _, pxy = sg.csd(x, y, fs=fs, window="hann", nperseg=nperseg, noverlap=noverlap,
                    detrend="constant", scaling="density")
    _, coh = sg.coherence(x, y, fs=fs, window="hann", nperseg=nperseg, noverlap=noverlap,
                          detrend="constant")
    eps = max(float(np.max(pxx)) * 1e-14, 1e-30)
    h = pxy / np.maximum(pxx, eps)
    lo, hi = float(band[0]), min(float(band[1]), 0.45 * fs)
    sel = (f >= lo) & (f <= hi)
    # Низкая coherence не маскируется: пользователь видит плохую достоверность.
    mag_db = db20(np.abs(h))
    phase_deg = np.degrees(np.unwrap(np.angle(h)))
    return {
        "freq": f[sel], "transfer_db": mag_db[sel], "phase_deg": phase_deg[sel],
        "coherence": coh[sel], "input_density_dbfs_hz": db10(pxx[sel]),
        "output_density_dbfs_hz": db10(pyy[sel]), "nperseg": nperseg,
        "segments_approx": max(1, int((n - noverlap) // (nperseg - noverlap))),
        "valid_fraction_coh_0_8": float(np.mean(coh[sel] >= 0.8)) if np.any(sel) else 0.0,
    }
