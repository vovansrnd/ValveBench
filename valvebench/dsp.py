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


def spectrum_view_limits(fs, preset=2):
    """Frequency limits for the live spectrum presets.

    Preset 0: conventional audio band (20 Hz–20 kHz).
    Preset 1: extended band (10 Hz–40 kHz).
    Preset 2: all positive frequencies through Nyquist (10 Hz–Fs/2).
    The upper edge is always clipped to the actual Nyquist frequency.
    """
    fs = float(fs)
    if not math.isfinite(fs) or fs <= 0:
        raise ValueError("Fs must be a finite positive number")
    nyquist = fs / 2.0
    if preset == 0:
        lo, hi = 20.0, min(20000.0, nyquist)
    elif preset == 1:
        lo, hi = 10.0, min(40000.0, nyquist)
    elif preset == 2:
        lo, hi = 10.0, nyquist
    else:
        raise ValueError(f"unknown spectrum band preset: {preset}")
    if hi <= lo:
        raise ValueError(f"Fs={fs:g} Hz is too low for spectrum band preset {preset}")
    return lo, hi


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


def spectrum_line_dbfs(block):
    """Sine-equivalent peak level of each FFT line; 0 dBFS is a full-scale sine."""
    return db20(block.amp)


def spectrum_asd_dbfs(block):
    """One-sided amplitude spectral density, dBFS/√Hz, relative to a full-scale sine."""
    return db10(2.0 * block.P / max(block.df, _TINY))


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


# ---------------------------------------------------- когерентные линии (rect)
#
# Когда ЦАП и АЦП тактируются одним кварцем (проверяется coh_err), тон — ровно
# целое число периодов в окне. Тогда прямоугольное окно не даёт утечки, а каждая
# гармоника занимает ОДИН бин: в линию попадает шум одного бина, а не 11 бинов
# лепестка BH (≈ −10 дБ), плюс усреднение M окон (−10·log10 M). Для петли
# E-MU это опускает измерительный пол гармоник с ≈ −117 до ≈ −139 дБc.

COH_TOL_BINS = 0.002       # допустимая расстройка тона относительно сетки бинов
MARGIN_OK_DB = 10.0        # консервативный порог для одного бина с поправкой на множественные проверки


def _rect_power(x):
    """Мощность (rms²) каждого бина при прямоугольном окне."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    P = 2.0 * np.abs(np.fft.rfft(x)) ** 2 / (n * n)
    P[0] *= 0.5
    if n % 2 == 0:
        P[-1] *= 0.5
    return P


def _local_floor(P, k, skip, half=40, guard=2):
    """Средняя мощность бина рядом с линией k без самих линий (skip — булева маска)."""
    lo, hi = max(1, k - half), min(P.size, k + half + 1)
    idx = np.arange(lo, hi)
    keep = (np.abs(idx - k) > guard) & ~skip[lo:hi]
    return float(np.mean(P[idx[keep]])) if np.any(keep) else 0.0


def _blocks(x, n):
    m = len(x) // n
    if m < 1:
        raise ValueError(f"запись короче одного окна: {len(x)} < {n}")
    return np.asarray(x[: m * n], dtype=np.float64).reshape(m, n)


def coherent_spur_profile(x, fs, n, min_blocks=3, coherence_min=None):
    """Оценить устойчивые одночастотные линии в записи тишины по DFT-бинам.

    Возвращает мощность *когерентных узкополосных составляющих* в каждом
    прямоугольном DFT-бине (RMS², в тех же единицах, что и мощность линии в
    analyze_tone), а также фазовую концентрацию. Широкополосный случайный шум
    не должен попадать в этот профиль: из |mean(C)|² вычитается ожидаемая
    шумовая составляющая mean(|C|²)/M. Линия принимается как стационарная,
    только если её фазовая концентрация >= coherence_min.

    Это дополнительный фон для гармонического детектора, не полноценная
    метрологическая оценка неопределённости. Требуются полные окна одинаковой
    длины n; для 1-Гц разрешения при 96 кГц обычно n=96000.
    """
    blocks = _blocks(np.asarray(x, dtype=np.float64), int(n))
    m = int(blocks.shape[0])
    if m < int(min_blocks):
        raise ValueError(f"для профиля устойчивых линий нужно минимум {min_blocks} блоков, получено {m}")
    if coherence_min is None:
        # При малом числе блоков случайные комплексные шумовые векторы могут
        # случайно выглядеть когерентными. Порог ужесточается для короткой
        # silent-записи, чтобы не объявлять случайный бин устойчивым spur.
        coherence_min = 0.99 if m <= 3 else (0.95 if m <= 5 else (0.90 if m <= 7 else 0.85))
    blocks = blocks - np.mean(blocks, axis=1, keepdims=True)
    X = np.fft.rfft(blocks, axis=1)
    C = (2.0 / n) * X
    if n % 2 == 0:
        C[:, -1] *= 0.5
    C[:, 0] *= 0.5
    p_each = (np.abs(C) ** 2) / 2.0
    mean_c = np.mean(C, axis=0)
    mean_p = np.mean(p_each, axis=0)
    coherence = np.abs(mean_c) / np.sqrt(np.maximum(np.mean(np.abs(C) ** 2, axis=0), 1e-300))
    # Смещение |mean(C)|², которое остаётся даже у чистого случайного шума.
    noise_bias = mean_p / max(m, 1)
    p_coherent = np.maximum((np.abs(mean_c) ** 2) / 2.0 - noise_bias, 0.0)
    p_coherent[coherence < float(coherence_min)] = 0.0
    return {
        "freq": np.fft.rfftfreq(int(n), 1.0 / float(fs)),
        "spur_power": p_coherent,
        "phase_coherence": coherence,
        "mean_bin_power": mean_p,
        "blocks": m,
        "df": float(fs) / int(n),
        "coherence_min": float(coherence_min),
    }


# --------------------------------------------------------------- один тон


def analyze_tone(x, fs, f0, n, nharm=9, band=(20.0, 20000.0), mains=50.0, half=HALF,
                 coherent_line_floors=None):
    """THD, THD+N, гармоники и фон по установившемуся тону.

    THD+N считается по усреднённым мощностям окон Блэкмана–Харриса; лепесток
    несущей исключается маской.

    При когерентном тоне гармоники оцениваются по прямоугольному DFT-бину.
    Комплексные коэффициенты гармоник каждого блока нормируются на фазу
    фундаментального тона (phi_h - h*phi_1), поэтому усреднение не теряет
    сигнал при фазовом сдвиге между границами блоков. Это не заменяет проверку
    когерентности частоты внутри окна: её по-прежнему задаёт coh_err.

    Локальный широкополосный пол для coherent-оценки — средняя мощность
    соседнего DFT-бина на один исходный блок, делённая на M для оценки среднего
    комплексного коэффициента. Дополнительно coherent_line_floors может
    передать мощность устойчивых одночастотных линий, заранее найденных в
    записи тишины на тех же DFT-бинах. Она НЕ делится на M: стабильный spur не
    исчезает от усреднения. Порог 10 dB — инженерный критерий обнаружения,
    а не формальный доверительный интервал.
    """
    blocks = _blocks(x, n)
    M = blocks.shape[0]
    raw = [Block(b, fs) for b in blocks]
    Pdir = np.mean([b.P for b in raw], axis=0)
    ref = raw[0]
    df = ref.df
    warn = []

    k0 = ref.k(f0)
    lo, hi = max(1, k0 - 3), min(Pdir.size, k0 + 4)
    if hi <= lo:
        raise ValueError("частота основного тона вне анализируемой полосы")
    k0 = lo + int(np.argmax(Pdir[lo:hi]))
    f_est = Block.centroid(_Shim(ref, Pdir), k0 * df)
    coh_err = f_est / df - round(f_est / df)

    half_f = min(half, max(1, (k0 - 1) // 2))
    nyq_lim = 0.45 * fs
    b_lo, b_hi = band[0], min(band[1], nyq_lim)
    coherent = abs(coh_err) <= COH_TOL_BINS and abs(f_est - k0 * df) <= COH_TOL_BINS * df
    harm = []
    masked = np.zeros(Pdir.size, dtype=bool)
    masked[max(0, k0 - half_f): k0 + half_f + 1] = True

    # Фаза несущей по каждому блоку — полезна и для когерентной оценки, и для
    # диагностики: низкая coherence может указывать на фазовые сбросы/потерю
    # отсчётов, даже если средний спектральный пик остался в нужном бине.
    phase_coherence = None
    phase_spread_deg = None

    if coherent:
        X = np.fft.rfft(blocks, axis=1)
        fundamental = (2.0 / n) * X[:, k0]
        fund_phase = np.angle(fundamental)
        phase_coherence = float(abs(np.mean(np.exp(1j * fund_phase))))
        phase_spread_deg = float(np.degrees(np.sqrt(max(0.0, -2.0 * np.log(max(phase_coherence, 1e-15))))))
        fund_amp = np.abs(fundamental)
        # Усредняем амплитуду, а не комплексный коэффициент: фазовые сдвиги
        # между блоками не должны искусственно уменьшать измеренный уровень.
        fund_peak = float(np.mean(fund_amp))
        Pf = fund_peak * fund_peak / 2.0

        # Средняя мощность одного исходного блока нужна для оценки шума в
        # одном DFT-бине. Среднее комплексных коэффициентов по M блокам имеет
        # шумовую мощность примерно в M раз ниже.
        Psingle = np.mean((2.0 * np.abs(X) ** 2 / (n * n)), axis=0)
        Psingle[0] *= 0.5
        if n % 2 == 0:
            Psingle[-1] *= 0.5
        hk = []
        for h in range(2, nharm + 1):
            fh = h * k0 * df
            if fh > b_hi:
                break
            kh = int(round(fh / df))
            if kh <= 0 or kh >= Psingle.size:
                break
            hk.append((h, fh, kh))

        skip = np.zeros(Psingle.size, dtype=bool)
        for kk in [k0] + [k for _, _, k in hk]:
            skip[max(0, kk - 2): min(Psingle.size, kk + 3)] = True
        for m_ in range(1, 7):
            km = int(round(m_ * mains / df)) if mains > 0 else 0
            if 0 < km < Psingle.size:
                skip[max(0, km - 2): min(Psingle.size, km + 3)] = True

        detected_power = 0.0
        raw_power = 0.0
        floors = []
        for h, fh, kh in hk:
            # Для нелинейного гармонического отклика фаза h-й гармоники
            # изменяется на h*phi1 при изменении фазы основного тона.
            # Удаляем эту фазу перед комплексным усреднением блоков.
            z = (2.0 / n) * X[:, kh] * np.exp(-1j * h * fund_phase)
            z_mean = np.mean(z)
            p_observed = float(abs(z_mean) ** 2 / 2.0)
            p_floor_one_block = _local_floor(Psingle, kh, skip)
            # Пол — по ТЕМ ЖЕ усреднённым комплексным коэффициентам соседних бинов (с той же
            # нормировкой фазы), а не «пол одного блока / M». Случайный шум при этом падает в M раз,
            # а детерминированная ошибка, одинаковая в каждом блоке (квантование периодичного
            # сигнала при f0 простой относительно Fs: период = окну), не падает и честно попадает
            # в пол. Иначе такие линии ложно признавались бы достоверными с вероятностью ~20 %.
            lo_k, hi_k = max(1, kh - 40), min(X.shape[1], kh + 41)
            nb = np.arange(lo_k, hi_k)
            keep = (np.abs(nb - kh) > 2) & ~skip[lo_k:hi_k]
            if np.any(keep):
                zn = (2.0 / n) * X[:, nb[keep]] * np.exp(-1j * h * fund_phase)[:, None]
                p_avg_nb = float(np.mean(np.abs(zn.mean(axis=0)) ** 2 / 2.0))
            else:
                p_avg_nb = p_floor_one_block / max(M, 1)
            p_random_mean = max(p_avg_nb, 1e-300)
            p_stable_spur = max(float((coherent_line_floors or {}).get(h, 0.0)), 0.0)
            p_noise_mean = p_random_mean + p_stable_spur
            margin = db10(p_observed / p_noise_mean) if p_observed > 0 else -300.0
            credible = bool(margin >= MARGIN_OK_DB)
            p_corrected = max(p_observed - p_noise_mean, 0.0)
            p_used = p_corrected if credible else p_observed
            raw_power += p_observed
            if credible:
                detected_power += p_corrected
            floors.append(p_noise_mean)

            # Фазовая концентрация гармоники после нормировки на h*phi1.
            z_rms = float(np.sqrt(np.mean(np.abs(z) ** 2)))
            harmonic_phase_coherence = float(abs(np.mean(z)) / z_rms) if z_rms > 0 else 0.0
            masked[max(0, kh - half_f): min(masked.size, kh + half_f + 1)] = True
            harm.append({
                "h": h, "f": fh,
                "dbc": db10(p_used / Pf) if p_used > 0 and Pf > 0 else -300.0,
                "pct": 100.0 * math.sqrt(p_used / Pf) if Pf > 0 else 0.0,
                "bound": not credible, "credible": credible,
                "raw_dbc": db10(p_observed / Pf) if p_observed > 0 and Pf > 0 else -300.0,
                "floor_dbc": db10(p_noise_mean / Pf) if p_noise_mean > 0 and Pf > 0 else -300.0,
                "random_floor_dbc": db10(p_random_mean / Pf) if p_random_mean > 0 and Pf > 0 else -300.0,
                "stable_spur_floor_dbc": db10(p_stable_spur / Pf) if p_stable_spur > 0 and Pf > 0 else None,
                "stable_spur_floor_dbfs": rms_dbfs(math.sqrt(p_stable_spur)) if p_stable_spur > 0 else None,
                "margin_db": margin,
                "phase_coherence": harmonic_phase_coherence,
            })

        thd = math.sqrt(max(detected_power, 0.0) / Pf) if Pf > 0 else 0.0
        thd_upper = math.sqrt(max(raw_power, 0.0) / Pf) if Pf > 0 else 0.0
        credible_count = sum(1 for hh in harm if hh["credible"])
        thd_bound = bool(hk) and credible_count == 0
        thd_partial = bool(hk) and 0 < credible_count < len(hk)
        line_floor_dbc = db10(float(np.mean(floors)) / Pf) if floors and Pf > 0 else None
        line_mode = "rect-phase-normalized"
        if phase_coherence is not None and phase_coherence < 0.8:
            warn.append(f"фаза несущей между блоками нестабильна (coherence={phase_coherence:.3f}); "
                        "гармоники усреднены с нормировкой на фазу несущей")
    else:
        if abs(coh_err) > 0.05:
            warn.append(f"тон не когерентен окну (сдвиг {coh_err:+.2f} бина): "
                        "гармоники рассчитаны по усреднённым мощностям окон Блэкмана–Харриса")
        else:
            warn.append(f"сдвиг тона {coh_err:+.4f} бина превышает допуск {COH_TOL_BINS:.3f}: "
                        "гармоники рассчитаны по усреднённым мощностям окон Блэкмана–Харриса")
        # В некогерентном режиме нельзя усреднять временные блоки: при фазовом
        # дрейфе это может погасить сигнал. Усредняем мощности спектров.
        fund_mask = np.zeros(Pdir.size, dtype=bool)
        fund_mask[max(0, k0 - half_f): min(Pdir.size, k0 + half_f + 1)] = True
        Pf = float(np.sum(Pdir[fund_mask]))
        if Pf <= 0:
            raise ValueError("тон не найден")
        ph = 0.0
        raw_power = 0.0
        for h in range(2, nharm + 1):
            fh = h * f_est
            if fh > b_hi:
                break
            kh = int(round(fh / df))
            a, b = max(0, kh - half_f), min(Pdir.size, kh + half_f + 1)
            p = float(np.sum(Pdir[a:b]))
            masked[a:b] = True
            ph += p
            raw_power += p
            harm.append({"h": h, "f": fh, "dbc": db10(p / Pf) if p > 0 else -300.0,
                         "pct": 100.0 * math.sqrt(p / Pf), "bound": False, "credible": None,
                         "raw_dbc": db10(p / Pf) if p > 0 else -300.0,
                         "floor_dbc": None, "margin_db": None, "phase_coherence": None})
        thd = math.sqrt(ph / Pf)
        thd_upper = thd
        thd_bound = False
        thd_partial = False
        line_floor_dbc = None
        line_mode = "bh-power"

    if Pf <= 0:
        raise ValueError("тон не найден")

    ka, kb = max(1, int(b_lo / df)), min(Pdir.size, int(b_hi / df) + 1)
    sel = np.zeros(Pdir.size, dtype=bool)
    sel[ka:kb] = True
    # THD+N = всё в полосе, кроме лепестка основного тона.
    fund_mask = np.zeros(Pdir.size, dtype=bool)
    fund_mask[max(0, k0 - half_f): min(Pdir.size, k0 + half_f + 1)] = True
    p_dn = float(np.sum(Pdir[sel & ~fund_mask]))
    p_n = float(np.sum(Pdir[sel & ~masked]))
    Pf_dir = float(np.sum(Pdir[fund_mask]))

    # Local SNR around the carrier is the appropriate validity metric for
    # frequency estimation. Broadband noise/hum elsewhere in the audio band
    # must not invalidate a clearly resolved 1 kHz clock pilot.
    local_radius = max(8, int(round(40.0 / df)))
    local_a = max(1, k0 - local_radius)
    local_b = min(Pdir.size, k0 + local_radius + 1)
    local_mask = np.zeros(Pdir.size, dtype=bool)
    local_mask[local_a:local_b] = True
    local_mask[fund_mask] = False
    for h in range(2, nharm + 1):
        kh = int(round(h * f_est / df))
        if 0 < kh < Pdir.size:
            local_mask[max(0, kh - 2):min(Pdir.size, kh + 3)] = False
    if mains > 0:
        for m_ in range(1, 7):
            km = int(round(m_ * mains / df))
            if local_a <= km < local_b:
                local_mask[max(0, km - 2):min(Pdir.size, km + 3)] = False
    local_bins = Pdir[local_mask]
    if local_bins.size:
        local_noise_power = float(np.median(local_bins) * (2 * half_f + 1))
        local_noise_power = max(local_noise_power, 1e-300)
        local_noise_dbfs = rms_dbfs(math.sqrt(local_noise_power))
        local_snr_db = db10(Pf_dir / local_noise_power) if Pf_dir > 0 else -300.0
    else:
        local_noise_dbfs = float("nan")
        local_snr_db = float("nan")

    hum = []
    if mains > 0:
        for m_ in range(1, 7):
            fm = m_ * mains
            if fm > b_hi or fm < b_lo:
                continue
            if any(abs(fm - h * f_est) < (half_f + 2) * df for h in range(1, nharm + 2)):
                continue
            km = int(round(fm / df))
            p = float(np.sum(Pdir[max(0, km - 3): min(Pdir.size, km + 4)]))
            hum.append({"f": fm, "dbfs": rms_dbfs(math.sqrt(p)),
                        "dbc": db10(p / Pf_dir) if Pf_dir > 0 else -300.0})

    spec_amp = np.sqrt(np.mean([b.amp ** 2 for b in raw], axis=0))
    level_power = Pf if line_mode.startswith("rect") else Pf
    return {
        "f0": f_est, "n": n, "blocks": M, "coh_err_bins": coh_err,
        "phase_coherence": phase_coherence, "phase_spread_deg": phase_spread_deg,
        "level_dbfs": rms_dbfs(math.sqrt(level_power)), "rms": math.sqrt(level_power),
        "harmonics": harm, "line_mode": line_mode, "line_floor_dbc": line_floor_dbc,
        "thd_pct": 100.0 * thd, "thd_db": db20(thd),
        "thd_upper_pct": 100.0 * thd_upper, "thd_upper_db": db20(thd_upper),
        "thd_bound": thd_bound, "thd_partial": thd_partial,
        "thdn_pct": 100.0 * math.sqrt(p_dn / Pf_dir) if Pf_dir > 0 else 0.0,
        "thdn_db": db10(p_dn / Pf_dir) if Pf_dir > 0 else -300.0,
        "noise_dbc": db10(p_n / Pf_dir) if Pf_dir > 0 else -300.0,
        "noise_dbfs": rms_dbfs(math.sqrt(p_n)),
        "local_noise_dbfs": local_noise_dbfs, "local_snr_db": local_snr_db,
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

    Мощности линий усредняются по отдельным блокам; фазовое взаимопогашение
    из-за независимых тактов ЦАП/АЦП не искажает численный результат.
    CCIF  : d2 = f2−f1, d3 = 2f1−f2, 2f2−f1, d5; проценты — к (A1+A2) (IEC 60268-3).
    SMPTE : боковые fв±m·fн, m=1..4; проценты — к амплитуде верхнего тона.
    """
    blocks = _blocks(x, n)
    # Do not average waveforms in the time domain: with independent DAC/ADC
    # clocks, the phase of each tone rotates between blocks and can cancel
    # almost completely (19/20 kHz are especially sensitive to 20-30 ppm).
    # Average spectral powers instead. The lobe integration tolerates the
    # small per-block bin offset while retaining phase-independent RMS.
    spectra = [Block(block, fs) for block in blocks]
    # Power-average magnitudes for the spectrum display as well: a waveform
    # average would still make drifting tones disappear from the displayed plot.
    spec_amp = np.sqrt(np.mean([blk.amp ** 2 for blk in spectra], axis=0))
    lo, hi = sorted((f1, f2))
    def amp(f):
        powers = [blk.lobe(f, half) for blk in spectra]
        return math.sqrt(float(np.mean(powers)))  # RMS line, power-averaged
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
        "freq": spectra[0].freq, "spec_db": db20(spec_amp),
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
