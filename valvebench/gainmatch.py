"""Gain Match: узкополосное измерение уровня тона и разности усиления каналов.

Почему не RMS по 120 мс. Широкополосный RMS включает шум, сетевой фон и
гармоники лампового каскада, а окно в 120 мс не содержит целого числа периодов.
Здесь уровень тона на частоте генератора определяется методом наименьших
квадратов (cos, sin, const) по окну, кратному 20 мс (целое число периодов
сети): шум и фон не попадают в результат, а ΔG устойчива до тысячных долей дБ.
Модуль не зависит от GTK и проверяется автоматически.

0.1.19 (достоверность):
* аппаратные события Rig.run (XRUN, перегрузка) — ФАТАЛЬНЫЕ: инвалидируют всю
  серию, пока нет привязки событий к отдельным сегментам;
* предупреждения analyze_tone() сохраняются в записи повторов (раньше терялись);
* ворота захвата — «несущая/негармонический остаток» (Carrier/Residual): шум и
  фон сети снижают это отношение, а гармоники DUT — нет, поэтому лампа с
  THD 5–8 % измеряется достоверно; SINAD/THD+N остались диагностикой и сами
  по себе каскад не бракуют;
* подоконный контроль стабильности несущей (100 мс): линейный тренд огибающей
  и робастный (MAD) выброс — ловят щелчки в окне, дрейф усиления и протечку
  чужого сегмента, которые проходят все спектральные ворота;
* контекстный TARE: обнуление хранит условия (частота, уровень, Fs, маршрут,
  устройство, микшер, калибровка) и сбрасывается при несовместимом контексте;
* профили частот: стандартный 100/1000/10000 и Mains Avoidance 94/994/9994
  (сеть 50 Гц; НЕ является рядом AES17);
* RIAA-компенсация уровня генератора (опция для фонокорректоров): уровень
  каждой частоты смещается по обратной RIAA-кривой, цифровой запас проверяется;
* repeats=1: повторяемость «не оценена», эталон петли не создаётся.
"""
from __future__ import annotations

import hashlib
import math
import re
import subprocess
import time
from collections import deque

import numpy as np

WINDOW_S = 0.5            # кратно 20 мс: целое число периодов 50 Гц (и 60 Гц)
RANGES = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)

SERIES_FREQUENCIES = (100.0, 1000.0, 10000.0)
MIN_SERIES_SNR_DB = 25.0          # устаревшее имя; SINAD — диагностика, см. ниже
DEFAULT_MATCH_TOL_DB = 0.20

# --- пороги достоверности 0.1.19 (обоснование: независимый аудит, 88 проверок) ---
CARRIER_RESIDUAL_MIN_DB = 30.0    # ворота захвата: несущая/негармонический остаток
SUBWINDOW_S = 0.1                 # подокно контроля огибающей (кратно 20 мс)
TREND_MAX = 0.005                 # размах линейного тренда огибающей (0.5 %)
Z_BURST_MAX = 5.0                 # робастный выброс остатка (MAD-z)
MIN_REPEATS_FOR_REFERENCE = 2     # эталон петли требует подтверждённой повторяемости

# Профили частот серии. Имена пользовательские, в интерфейсе показываются
# фактические частоты. «Mains Avoidance» — обход сетевых гармоник 50 Гц
# (расстояние ≥6 бинов при df=1 Гц); НЕ является рядом AES17.
FREQUENCY_PROFILES = {
    "standard": ("Стандартный (100 / 1000 / 10000 Гц)", (100.0, 1000.0, 10000.0)),
    "mains_avoidance_50": ("Обход сети 50 Гц (94 / 994 / 9994 Гц)", (94.0, 994.0, 9994.0)),
}

FATAL_WARNING_RE = re.compile(r"underrun|overrun|xrun|ПЕРЕГРУЗКА", re.IGNORECASE)

TARE_CONTEXT_KEYS = ("freq", "level_dbfs", "fs", "route", "device_key",
                     "mixer_hash", "cal_sig")


def tone_fit(x, fs, f):
    """(пиковая амплитуда, фаза рад, rms остатка) тона частоты f в x (LSQ cos/sin/const)."""
    x = np.asarray(x, dtype=np.float64)
    t = np.arange(x.size) / float(fs)
    w = 2.0 * math.pi * f
    a = np.column_stack((np.cos(w * t), np.sin(w * t), np.ones_like(t)))
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    res = x - a @ coef
    amp = math.hypot(coef[0], coef[1])
    return amp, math.atan2(-coef[1], coef[0]), float(np.sqrt(np.mean(res * res)))


def to_db(a):
    return 20.0 * math.log10(max(a, 1e-12))


def rms_dbfs(x):
    """RMS → dBFS по конвенции полной синусоиды (0 dBFS = полный синус)."""
    return 20.0 * math.log10(max(float(x), 1e-12) * math.sqrt(2.0))


from collections import OrderedDict as _OrderedDict

# LRU-кеши псевдообратных матриц, РАЗДЕЛЬНЫЕ по семействам: основной
# (до H9) ограничен восемью профилями И бюджетом 64 МиБ; подоконный —
# восемью профилями и 4 МиБ. Ограничение по байтам учитывает Fs и размер окна.
_PINV_CACHE = _OrderedDict()
_PINV_CACHE_MAX = 8
# Ограничиваем не только количество профилей, но и фактический объём матриц.
# При Fs=192 кГц и H1…H9 один профиль (A + pinv) занимает около 27.8 МиБ.
_PINV_CACHE_BUDGET = 64 * 1024 * 1024
_SUB_CACHE = _OrderedDict()
_SUB_CACHE_MAX = 8
_SUB_CACHE_BUDGET = 4 * 1024 * 1024
_PINV_STATS = {"hit": 0, "miss": 0}


def _cache_nbytes(value):
    if isinstance(value, tuple):
        return sum(int(getattr(v, "nbytes", 0)) for v in value)
    return int(getattr(value, "nbytes", 0))


def _cache_used(store):
    return sum(_cache_nbytes(value) for value in store.values())


def _cache_get(store, key, max_len):
    got = store.get(key)
    if got is not None:
        store.move_to_end(key)
        _PINV_STATS["hit"] += 1
        return got
    _PINV_STATS["miss"] += 1
    return None


def _cache_put(store, key, value, max_len):
    budget = _PINV_CACHE_BUDGET if store is _PINV_CACHE else _SUB_CACHE_BUDGET
    size = _cache_nbytes(value)
    # Не вытесняем полезный кеш ради профиля, который всё равно не помещается.
    if size > budget:
        return
    if key in store:
        del store[key]
    used = _cache_used(store)
    while store and (len(store) >= max_len or used + size > budget):
        _old_key, old_value = store.popitem(last=False)
        used -= _cache_nbytes(old_value)
    store[key] = value


def _harmonic_basis(fs, f, n, nharm):
    """(pinv, A) базиса тона+гармоник; LRU-кеш по (fs, f, n, nharm)."""
    key = (int(fs), round(float(f), 6), int(n), int(nharm))
    got = _cache_get(_PINV_CACHE, key, _PINV_CACHE_MAX)
    if got is not None:
        return got
    t = np.arange(n) / float(fs)
    w = 2.0 * math.pi * f
    cols = [np.ones(n)]
    for h in range(1, nharm + 1):
        cols += [np.cos(h * w * t), np.sin(h * w * t)]
    A = np.column_stack(cols)
    # A is tall (N×19). Form the tiny Gram matrix and compute its inverse
    # pseudo-inverse instead of an SVD of the full tall matrix: for this
    # normalized harmonic basis stayed well-conditioned in the tested domain
    # (cond(A) <= 1.42 from 5 Hz through the supported audio band); cache misses
    # become much cheaper without changing the least-squares solution.
    gram = A.T @ A
    pinv = np.linalg.pinv(gram, rcond=1e-12) @ A.T
    got = (pinv, A)
    _cache_put(_PINV_CACHE, key, got, _PINV_CACHE_MAX)
    return got


def tone_fit_harmonics(x, fs, f, nharm=9, f_max=None, f_band_hi=20000.0,
                       include_sinad=True):
    """LSQ-аппроксимация тона + гармоник h=1..nharm одним базисом.

    Гармоники выше f_max (по умолчанию 0.45·Fs) в базис НЕ включаются:
    их алиасы — реальные сигналы в полосе, вычитать их из остатка нельзя
    (семантика согласована со статическим анализатором).

    Возвращает (амплитуда тона, фаза тона, rms НЕГАРМОНИЧЕСКОГО остатка
    в полосе [20 Гц, f_band_hi], rms полного остатка). Негармонический
    остаток — основа ворот Carrier/Residual живого индикатора.
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if f_max is None:
        f_max = 0.45 * fs
    f_band_hi = min(float(f_band_hi), 0.45 * fs)
    # Для C/R вычитаем только гармоники, попадающие в анализируемую полосу.
    # Static masks H2…Hn до band_hi; fitting внеполосных гармоник мог бы
    # поглотить часть внутриполосной помехи при неидеальной ортогональности.
    harmonic_hi = min(float(f_max), f_band_hi)
    nharm_eff = max(0, min(int(nharm), int(harmonic_hi / f) if f > 0 else 0))
    pinv, A = _harmonic_basis(fs, f, n, nharm_eff)
    coef = pinv @ x
    res_nh = x - A @ coef
    tone_part = A[:, :3] @ coef[:3]           # const + cos/sin первой гармоники
    res_full = x - tone_part if include_sinad else None
    amp = math.hypot(coef[1], coef[2])
    phase = math.atan2(-coef[2], coef[1])
    # И C/R, и SINAD считаются в одной полосе [20 Гц, f_band_hi], как
    # статический анализатор. До R5 Live SINAD дополнительно включал DC,
    # инфранизкие частоты и ультразвук за пределами полосы Static.
    spec_nh = np.fft.rfft(res_nh)
    fr = np.fft.rfftfreq(n, 1.0 / fs)
    band = (fr >= 20.0) & (fr <= f_band_hi)
    # Верхняя граница ≤0.45·Fs исключает Найквист; нижняя исключает DC.
    # Для одностороннего спектра Parseval: rms² = 2·Σ|X_k|² / n².
    if band.any():
        rms_nh = float(math.sqrt(2.0 * float(np.sum(np.abs(spec_nh[band]) ** 2))) / n)
        if include_sinad:
            spec_full = np.fft.rfft(res_full)
            rms_full = float(math.sqrt(2.0 * float(np.sum(np.abs(spec_full[band]) ** 2))) / n)
        else:
            rms_full = 0.0
    else:
        rms_nh = rms_full = 0.0
    return amp, phase, rms_nh, rms_full


def pick_range(delta_db, tol_db):
    """Полуширина шкалы ΔG: минимальная из ряда, где стрелка остаётся в пределах 80 %."""
    need = max(abs(delta_db) / 0.8, 3.0 * tol_db)
    for r in RANGES:
        if r >= need:
            return r
    return RANGES[-1]


def classify(delta_db, tol_db):
    """'ok' внутри допуска, 'near' в пределах трёх допусков, иначе 'far'."""
    d = abs(delta_db)
    return "ok" if d <= tol_db else ("near" if d <= 3.0 * tol_db else "far")


def calc_carrier_residual_db(level_dbfs, analysis):
    """Carrier / non-harmonic residual ratio, including reported mains hum.

    ``dsp.analyze_tone`` exposes broadband/non-harmonic noise in ``noise_dbfs``
    and removes the first six mains components from that floor because hum is
    reported separately. Live LSQ keeps those hum components in its residual.
    Add the reported hum powers back here so Static and Live use the same gate
    semantics (carrier divided by noise + hum, with DUT harmonics excluded).
    """
    powers = []
    noise_dbfs = analysis.get("noise_dbfs")
    try:
        value = float(noise_dbfs)
        if math.isfinite(value):
            powers.append(10.0 ** (value / 10.0))
    except (TypeError, ValueError):
        pass
    for item in analysis.get("hum", ()) or ():
        try:
            value = float(item.get("dbfs"))
            if math.isfinite(value):
                powers.append(10.0 ** (value / 10.0))
        except (AttributeError, TypeError, ValueError):
            continue
    residual_dbfs = 10.0 * math.log10(max(math.fsum(powers), 1e-30))
    return float(level_dbfs) - residual_dbfs


# ------------------------------------------------------------------ достоверность

def envelope_stability(win, fs, f0, sub_s: float = SUBWINDOW_S) -> dict:
    """Стабильность огибающей несущей по подокнам (LS-амплитуда на f0).

    Возвращает словарь: trend_span — размах линейного тренда амплитуд
    относительно среднего; z_burst — робастный выброс максимального остатка
    (отклонение от линейного тренда, нормировка MAD). Ловит щелчки внутри окна,
    дрейф усиления и протечку чужого сегмента, которые проходят спектральные
    ворота (у них нет транзиентов или нет энергии вне несущей).
    """
    win = np.asarray(win, dtype=np.float64)
    n_sub = int(round(sub_s * fs))
    if n_sub < 16 or win.size < 4 * n_sub:
        return {"trend_span": 0.0, "z_burst": 0.0, "subwindows": 0}
    sub_key = (int(fs), round(float(f0), 6), int(n_sub))
    got = _cache_get(_SUB_CACHE, sub_key, _SUB_CACHE_MAX)
    if got is not None:
        pinv = got
    else:
        # порядок колонок ЕДИНЫЙ с _harmonic_basis: [ones, cos, sin] —
        # иначе hypot(coef[1], coef[2]) извлекает sin-компоненту и константу
        t = np.arange(n_sub) / float(fs)
        w = 2.0 * math.pi * f0
        basis = np.column_stack((np.ones(n_sub), np.cos(w * t), np.sin(w * t)))
        pinv = np.linalg.pinv(basis)
        _cache_put(_SUB_CACHE, sub_key, pinv, _SUB_CACHE_MAX)
    # порядок колонок базиса: [ones, cos, sin] → амплитуда = hypot(coef[1], coef[2])
    amps = []
    for s in range(0, win.size - n_sub + 1, n_sub):
        seg = win[s:s + n_sub]
        coef = pinv @ seg
        amps.append(math.hypot(coef[1], coef[2]))
    amps = np.asarray(amps)
    if amps.size < 4:
        return {"trend_span": 0.0, "z_burst": 0.0, "subwindows": int(amps.size)}
    mean_amp = max(float(np.mean(amps)), 1e-300)
    k = np.arange(amps.size, dtype=float)
    line = np.polyfit(k, amps, 1)
    trend = np.polyval(line, k)
    resid = amps - trend
    trend_span = float((trend.max() - trend.min()) / mean_amp)
    med = float(np.median(resid))
    mad = 1.4826 * float(np.median(np.abs(resid - med)))
    # Пол делителя — от амплитуды, а не от MAD: на идеально плоском сигнале MAD
    # вырождается в шум округления float, и один LSB даёт огромный z. Физический
    # выброс (≥0.5 % амплитуды) при таком поле по-прежнему даёт z ≥ 5000.
    # Симметрично: ловятся и положительные всплески, и кратковременные ПРОВАЛЫ.
    dev = max(float(resid.max() - med), float(med - resid.min()))
    z = float(dev / max(mad, mean_amp * 1e-6))
    return {"trend_span": trend_span, "z_burst": z, "subwindows": int(amps.size)}


def classify_warnings(warnings) -> dict:
    """Разделение предупреждений стенда: фатальные аппаратные и информационные.

    Фатальные (XRUN воспроизведения/захвата, перегрузка АЦП) инвалидируют серию.
    Информационные (например, переход analyze_tone в окно Блэкмана–Харриса)
    сохраняются, но не бракуют измерение.
    """
    fatal, info = [], []
    for w in warnings or []:
        s = str(w)
        (fatal if FATAL_WARNING_RE.search(s) else info).append(s)
    return {"fatal": fatal, "info": info}


def mains_proximity_warnings(freqs, mains_hz: float, df_hz: float = 1.0,
                             min_bins: float = 6.0, max_harmonic: int = 400) -> list[str]:
    """Информационные предупреждения о близости точек ряда к гармоникам сети.

    Диапазон гармоник — до максимальной частоты ряда (коллизия 9994 Гц со
    200-й гармоникой 50 Гц реальна: 200×49.97=9994). Линия в маске несущей
    (±5 бинов) спектральными воротами не обнаруживается — единственная защита
    выбор частот.
    """
    out = []
    mains_hz = float(mains_hz)
    if mains_hz <= 0:
        return out
    if max_harmonic is None:
        max_harmonic = 400
    k_max = min(int(max_harmonic), int(math.floor(max(freqs) / mains_hz) + 1))
    # альтернативные точки других профилей: совет уместен, только если замена
    # реально уводит от данной гармоники
    alternatives = sorted({round(float(g), 6) for name, (_, g) in FREQUENCY_PROFILES.items()
                           for g in g} - {round(float(f), 6) for f in freqs})
    for f in freqs:
        for k in range(1, k_max + 1):
            fh = k * mains_hz
            if fh <= 0 or fh > 0.45 * 200000:
                continue
            distance_bins = abs(f - fh) / float(df_hz)
            if distance_bins < min_bins:
                alt = next((g for g in alternatives
                            if abs(g - fh) / float(df_hz) >= min_bins), None)
                tail = (f" — рассмотрите частоту {alt:g} Гц" if alt
                        else " — измените частоту или убедитесь в малости фона сети")
                out.append(
                    f"частота {f:g} Гц на {distance_bins:.1f} бина от {k}-й гармоники "
                    f"сети ({fh:g} Гц): несимметричный фон сети попадает в вектор "
                    f"несущей и спектральными воротами не обнаруживается{tail}")
                break
    return out


def estimate_signal_frequency(x, fs, f_nominal, n_blocks=4):
    """Оценка фактической частоты тона медианным фазовым набегом между
    соседними подотрезками (идея из независимого форка, robust_frequency).

    Расстройка тактов ЦАП/АЦП (десятки ppm) при LSQ на номинальной частоте
    попадает в остаток и ложно снижает C/R (на 10 кГц 21 ppm → ~14 дБ).
    Медиана переживает одиночный фазовый скачок (остановку потока) и шум.
    Возвращает (f_est, reliable)."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    m = n // n_blocks
    if m < 16 or n_blocks < 3:
        return float(f_nominal), False
    # Демодуляция по ГЛОБАЛЬНОМУ времени: при локальном времени подотрезка
    # фазовый набег между блоками содержал алиас несущей (2π·f_real·T_blk mod 2π),
    # а не расстройку тактов — оценка частоты срывалась (регрессия U1).
    t_glob = np.arange(n_blocks * m) / float(fs)
    z = x[:n_blocks * m] * np.exp(-2j * np.pi * f_nominal * t_glob)
    zb = z.reshape(n_blocks, m).mean(axis=1)
    if float(np.max(np.abs(zb))) < 1e-9:
        return float(f_nominal), False
    ph = np.angle(zb)
    d = np.diff(np.unwrap(ph))
    if float(np.median(np.abs(d))) > 2.5:
        return float(f_nominal), False   # расхождение слишком велико для модели
    f_est = float(f_nominal + np.median(d) / (2.0 * math.pi * m / fs))
    if not math.isfinite(f_est) or f_est <= 0:
        return float(f_nominal), False
    return f_est, True


class GainTracker:
    """Скользящая оценка ΔG = G(L) − G(R) по живому захвату.

    TARE хранит контекст (см. TARE_CONTEXT_KEYS). При несовместимом контексте
    (изменилась частота, уровень, Fs, маршрут, карта, микшер или калибровка)
    обнуление сбрасывается автоматически, причина сообщается. Контекстная
    проверка не обнаруживает самопроизвольный аналоговый дрейф DUT.
    """

    def __init__(self, tau=1.5, hist_s=3.0):
        self.tau = tau
        self.hist_s = hist_s
        self.tare_db = 0.0
        self.tare_ctx = None
        self.reset()

    def reset(self):
        self.filt = None
        self.hist = deque()
        self.last_t = None
        self.last_raw = None

    def tare(self, value=None, ctx=None):
        """Принять текущую разность за ноль (петля или заведомо одинаковые каналы).

        ctx — условия обнуления (словарь по TARE_CONTEXT_KEYS); без контекста
        сброс по несовместимости невозможен.
        """
        if value is None:
            value = self.last_raw
        if value is not None:
            # "Принять текущее значение за ноль" задаёт новую опорную точку.
            # Нельзя накапливать поправку при повторном нажатии: это сдвинет
            # индикатор в противоположную сторону на уже учтённую ΔG.
            self.tare_db = float(value)
            self.tare_ctx = dict(ctx) if ctx else None
            self.reset()
        return self.tare_db

    def clear_tare(self):
        self.tare_db = 0.0
        self.tare_ctx = None
        self.reset()

    @staticmethod
    def tare_mismatch_reason(stored: dict | None, ctx: dict | None) -> str | None:
        """Человекочитаемая причина несовместимости контекста TARE (или None)."""
        if not stored or not ctx:
            return None
        labels = {"freq": "частота", "level_dbfs": "уровень генератора",
                  "fs": "частота дискретизации", "route": "маршрут",
                  "device_key": "устройство", "mixer_hash": "настройки микшера",
                  "cal_sig": "калибровка"}
        changed = []
        for k in TARE_CONTEXT_KEYS:
            if k not in stored or k not in ctx:
                # нет данных об условии — контекст несовместим (безопасный сброс)
                changed.append(labels[k] + " (нет данных)")
            elif stored[k] != ctx[k]:
                changed.append(labels[k])
        return ", ".join(changed) if changed else None

    def update(self, ring, fs, f, gen_dbfs, route, cal=None, now=0.0, ctx=None):
        """ring — последний захват (N, 2). Возвращает словарь состояния для индикатора.

        ctx — текущие условия (те же ключи, что у tare): при несовместимости
        старое обнуление сбрасывается с сообщением.
        """
        n = int(round(WINDOW_S * fs))
        if route != 0:
            self.reset()
            return {"valid": False, "msg": "ΔG: подай тест в оба канала (L + R)"}
        if ring is None or len(ring) < n:
            self.reset()
            return {"valid": False, "msg": "накопление данных…"}
        mismatch = self.tare_mismatch_reason(self.tare_ctx, ctx)
        reset_msg = None
        if mismatch:
            reset_msg = f"TARE сброшен автоматически: изменились условия ({mismatch})"
            self.clear_tare()
        seg = ring[-n:]
        try:
            band_hi = float(cal.get("band_hi", 20000.0)) if cal is not None else 20000.0
        except AttributeError:                 # duck-typed cal без словарного интерфейса
            band_hi = 20000.0
        band_hi = min(band_hi, 0.45 * fs)

        def _rms_nh(fc, ch):
            return tone_fit_harmonics(seg[:, ch], fs, fc, nharm=9, f_max=0.45 * fs,
                                      f_band_hi=band_hi, include_sinad=False)[2]

        # Фактическая частота: расстройка тактов (десятки ppm) при LSQ на
        # номинале попадает в остаток и ложно снижает C/R (10 кГц, 21 ppm →
        # ~14 дБ). Оценка по медианному фазовому набегу может сорваться на
        # мультилинейном сигнале (гребёнка гармоник, близкая линия), поэтому
        # частота выбирается по МИНИМУМУ негармонического остатка между
        # номиналом и оценкой — метрологически осмысленный критерий.
        # Кандидаты нормализуются ДО оценки остатка. Раньше округление было
        # только после min(...): шумовая f_est гуляла на ~1e-3 Гц, а _rms_nh()
        # успевал создавать новый ключ pinv-кеша с точностью 1e-6 на каждом тике.
        f_nom = round(float(f), 2)
        candidates = [f_nom]
        f_est, f_ok = estimate_signal_frequency(seg[:, 0], fs, f)
        if not f_ok:
            # Используем оценку второго канала, если первый не дал надёжного
            # результата. Важно сохранить и его флаг успеха.
            f_est, f_ok = estimate_signal_frequency(seg[:, 1], fs, f)
        f_est_q = round(float(f_est), 2) if f_ok else f_nom
        # Порог вставки после квантования — половина шага 0.01 Гц.
        if f_ok and abs(f_est_q - f_nom) >= 0.01:
            candidates.insert(0, f_est_q)
        f_fit = min(candidates, key=lambda fc: _rms_nh(fc, 0))
        amps, phs, res_nh, res_full, stab = [], [], [], [], []
        for c in (0, 1):
            a, ph, r_nh, r_full = tone_fit_harmonics(seg[:, c], fs, f_fit, nharm=9,
                                                     f_max=0.45 * fs, f_band_hi=band_hi)
            amps.append(a); phs.append(ph)
            res_nh.append(r_nh); res_full.append(r_full)
            stab.append(envelope_stability(seg[:, c], fs, f_fit))
        lv = [to_db(a / 1.0) for a in amps]               # dBFS пика синуса (0 dBFS = полный синус)
        if min(amps) < 1e-6:
            self.reset()
            return {"valid": False, "msg": "на входе нет тона: проверь кабели и частоту"}
        # C/R — несущая/негармонический остаток: гармоники DUT в базисе вычтены,
        # ламповый каскад с THD 5–10 % не бракуется (та же семантика, что в серии)
        carrier_residual = [to_db(a) - rms_dbfs(r) for a, r in zip(amps, res_nh)]
        # SINAD (тон/полный остаток, включая гармоники) — диагностика, не бракует:
        # 20lg((A/√2)/r_full) — отношение RMS тона к RMS полного остатка
        sinad = [to_db(a / math.sqrt(2.0)) - to_db(r) for a, r in zip(amps, res_full)]
        # порядок: сначала целостность окна (сообщение action-ориентировано),
        # затем C/R — когерентный всплеск поднимает и негармонический остаток
        bad_stab = max(s["trend_span"] for s in stab) > TREND_MAX or \
            max(s["z_burst"] for s in stab) > Z_BURST_MAX
        if bad_stab:
            self.reset()
            return {"valid": False,
                    "msg": "несущая нестабильна в окне (щелчок, дрейф усиления или "
                           "биения близкой помехи) — показание недостоверно"}
        if min(carrier_residual) < CARRIER_RESIDUAL_MIN_DB:
            self.reset()
            return {"valid": False,
                    "msg": (f"недостаточный C/R — несущая/негармонический остаток "
                            f"({min(carrier_residual):.1f} dB; нужно ≥{CARRIER_RESIDUAL_MIN_DB:.0f} dB): "
                            f"шум или фон сети слишком близко к тону")}
        gains = None
        calibrated = False
        if cal is not None:
            try:
                g = [cal.gain_db(float(gen_dbfs), lv[c], c) for c in (0, 1)]
                if all(v is not None for v in g):
                    gains, calibrated = g, True
            except Exception:                              # старый профиль без нужных полей
                gains = None
        raw = (gains[0] - gains[1]) if calibrated else (lv[0] - lv[1])
        self.last_raw = raw
        delta = raw - self.tare_db
        # фильтр и разброс
        if self.filt is None or self.last_t is None:
            self.filt = delta
        else:
            dt = max(0.0, now - self.last_t)
            al = 1.0 - math.exp(-dt / self.tau)
            self.filt += al * (delta - self.filt)
        self.last_t = now
        self.hist.append((now, delta))
        while self.hist and now - self.hist[0][0] > self.hist_s:
            self.hist.popleft()
        vals = np.array([v for _, v in self.hist])
        sigma = float(np.std(vals)) if len(vals) > 5 else None
        dphi = math.degrees(((phs[0] - phs[1] + math.pi) % (2 * math.pi)) - math.pi)
        out = {"valid": True, "delta": delta, "filt": self.filt, "sigma": sigma,
               "level_dbfs": lv, "snr_db": sinad, "sinad_db": sinad,
               "carrier_residual_db": carrier_residual, "envelope": stab,
               "freq_est_hz": f_fit, "freq_offset_hz": f_fit - f,
               "phase_deg": dphi,
               "gain_db": gains, "calibrated": calibrated, "tare_db": self.tare_db,
               "msg": ""}
        if reset_msg:
            out["msg"] = reset_msg
            out["tare_reset_reason"] = mismatch
        return out


def live_xrun_state(prev_xruns, prev_at, now, xruns, hold_s=1.0):
    """Состояние XRUN в Live (R5).

    Любое изменение счётчика — увеличение ИЛИ сброс при новом процессе/сессии —
    инвалидирует текущую оценку на hold_s. Начальное ненулевое значение тоже
    считается событием: нельзя принимать неизвестно когда повреждённый поток.
    Возвращает (invalid, new_xruns, new_at)."""
    xruns = None if xruns is None else int(xruns)
    at = prev_at
    if xruns is None:
        return False, prev_xruns, at
    if prev_xruns is None:
        if xruns > 0:
            return True, xruns, now
        return False, xruns, at
    if xruns != prev_xruns:
        return True, xruns, now
    invalid = at is not None and (now - at) < hold_s
    return bool(invalid), xruns, at


def live_fresh_frames(capture_frames, baseline_frames):
    """Number of capture frames received since the last transport fault, or None."""
    if capture_frames is None or baseline_frames is None:
        return None
    try:
        return max(0, int(capture_frames) - int(baseline_frames))
    except (TypeError, ValueError):
        return None


def live_fresh_window_ready(fresh_frames, fs, required_s=0.6):
    """True only after a complete fresh capture interval has arrived."""
    if fresh_frames is None or int(fs) <= 0:
        return False
    return int(fresh_frames) >= int(round(float(required_s) * int(fs)))


def live_transport_problem(status):
    """Return a user-facing fault when the Live capture/playback is not healthy."""
    status = status or {}
    if status.get("running") is False:
        return "аудиопоток остановлен"
    if status.get("capture_alive") is False:
        return "процесс захвата АЦП завершился"
    if status.get("playback_alive") is False:
        return "процесс воспроизведения ЦАП завершился"
    if status.get("capture_stale") is True:
        age = status.get("capture_age_s")
        detail = f" ({float(age):.1f} с без новых отсчётов)" if age is not None else ""
        return "нет свежих отсчётов АЦП" + detail
    if status.get("playback_stale") is True:
        age = status.get("playback_age_s")
        detail = f" ({float(age):.1f} с без передачи данных)" if age is not None else ""
        return "нет продвижения потока ЦАП" + detail
    return None


def device_key_from_hw(hw):
    """Stable physical identity; unlike hw:N, it survives ALSA card re-numbering."""
    try:
        from .backend import stable_device_identity
        return stable_device_identity(hw)
    except Exception:
        value = str(hw or "sim")
        if value == "sim":
            return "sim"
        match = re.fullmatch(r"hw:(\d+)(?:,(\d+))?", value)
        if not match:
            return value
        card, pcm = int(match.group(1)), int(match.group(2) or 0)
        try:
            card_id = open(f"/proc/asound/card{card}/id", encoding="utf-8").read().strip()
        except OSError:
            card_id = f"card{card}"
        return f"alsa:{card_id}:pcm{pcm}"


def _device_description(backend):
    hw = getattr(backend, "hw", None)
    if hw:
        return str(hw)
    return getattr(backend, "name", type(backend).__name__)


def mixer_fingerprint_from_hw(hw):
    """Hash ALSA controls so a loopback reference cannot survive mixer changes."""
    match = re.fullmatch(r"hw:(\d+)(?:,\d+)?", str(hw or ""))
    if not match:
        return None
    try:
        proc = subprocess.run(["amixer", "-c", match.group(1), "contents"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=3.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def riaa_level_adjust_db(f, iec=False):
    """Смещение уровня генератора для компенсации RIAA-каскада, дБ.

    Каскад с кривой playback RIAA на 100 Гц усиливает на +13.1 дБ — генератор
    должен дать на 13.1 дБ тише; на 10 кГц — на 13.7 дБ громче.
    Знак: level(f) = база − riaa_playback_db(f).
    """
    from . import dsp as _dsp
    return -float(_dsp.riaa_playback_db(np.atleast_1d(float(f)), iec)[0])


def _reference_map(reference):
    return {round(float(row["frequency_hz"]), 6): row for row in reference.get("rows", [])}


def validate_loopback_reference(rig, reference, level_dbfs, frequencies=SERIES_FREQUENCIES,
                                riaa_compensate=False):
    """Reject stale or mismatched reference rather than silently applying it."""
    if not isinstance(reference, dict) or reference.get("kind") != "gain_match_loopback_reference":
        raise ValueError("нет эталона петли карты: сначала выполни шаг 1")
    backend = rig.be
    key = device_key_from_hw(getattr(backend, "hw", "sim"))
    if reference.get("device_key") != key:
        raise ValueError("эталон петли создан для другой звуковой карты; повтори шаг 1")
    if int(reference.get("repeats", 0)) < MIN_REPEATS_FOR_REFERENCE:
        raise ValueError("эталон петли создан без подтверждённой повторяемости; повтори шаг 1")
    expected_mixer = reference.get("mixer_fingerprint")
    if expected_mixer:
        current_mixer = mixer_fingerprint_from_hw(getattr(backend, "hw", None))
        if current_mixer is None:
            raise ValueError("не удалось проверить настройки ALSA-микшера; повтори шаг 1")
        if current_mixer != expected_mixer:
            raise ValueError("настройки микшера звуковой карты изменились после петли; повтори шаг 1")
    if int(reference.get("fs", -1)) != int(rig.fs):
        raise ValueError("частота дискретизации изменилась; повтори шаг 1")
    if abs(float(reference.get("generator_level_dbfs", 999.0)) - float(level_dbfs)) > 1e-6:
        raise ValueError("уровень генератора изменился; для точной компенсации повтори шаг 1")
    if bool(reference.get("riaa_compensated", False)) != bool(riaa_compensate):
        raise ValueError("режим RIAA-компенсации эталона и измерения различаются; повтори шаг 1")
    rows = _reference_map(reference)
    wanted = {round(float(f), 6) for f in frequencies}
    if set(rows) != wanted:
        raise ValueError("набор частот эталона не совпадает с измерением; повтори шаг 1")
    bad = [f for f, row in rows.items() if not row.get("valid", False)]
    if bad:
        raise ValueError("эталон петли содержит невалидные измерения; повтори шаг 1")
    return rows


def build_loopback_reference(result):
    """Create a frequency-dependent L/R correction from a direct DAC→ADC loop."""
    if result.get("purpose") != "loopback":
        raise ValueError("эталон можно создать только из измерения прямой петли ЦАП→АЦП")
    if int(result.get("repeats", 0)) < MIN_REPEATS_FOR_REFERENCE:
        raise ValueError("повторяемость петли не оценена: нужен минимум "
                         f"{MIN_REPEATS_FOR_REFERENCE} повтора (сейчас {result.get('repeats')})")
    rows = []
    for row in result.get("rows", []):
        if not row.get("valid"):
            raise ValueError(f"петля на {row['frequency_hz']:.0f} Гц измерена ненадёжно")
        if row.get("repeat_stable") is not True:
            raise ValueError(f"петля на {row['frequency_hz']:.0f} Гц нестабильна; проверь соединения и повтори")
        rows.append({
            "frequency_hz": float(row["frequency_hz"]),
            "delta_adc_db": float(row["delta_adc_db"]),
            "repeat_spread_db": float(row["delta_output_spread_db"]),
            "snr_l_db": float(row["channels"]["L"]["snr_db"]),
            "snr_r_db": float(row["channels"]["R"]["snr_db"]),
            "valid": True,
        })
    if len(rows) != len(result.get("frequencies_hz", [])) or not rows:
        raise ValueError("эталон петли неполон")
    return {
        "kind": "gain_match_loopback_reference",
        "schema": 2,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_key": result["device_key"],
        "device": result["device"],
        "backend": result["backend"],
        "mixer_fingerprint": result.get("mixer_fingerprint"),
        "fs": int(result["fs"]),
        "generator_level_dbfs": float(result["generator_level_dbfs"]),
        "riaa_compensated": bool(result.get("riaa_compensated", False)),
        "repeatability": result.get("repeatability", "assessed"),
        "repeats": int(result.get("repeats", 0)),
        "frequencies_hz": list(result["frequencies_hz"]),
        "rows": rows,
        "instruction": "Direct DAC L→ADC L and DAC R→ADC R loop; repeat after mixer, cable, device or rate changes.",
    }


def measure_series(rig, frequencies=SERIES_FREQUENCIES, level_dbfs=-30.0, repeats=3,
                   metadata=None, purpose="loopback", reference=None,
                   riaa_compensate=False, frequency_profile=None):
    """Measure three tones for either loopback correction or DUT channel matching.

    Step 1 (purpose='loopback'): connect DAC L→ADC L and DAC R→ADC R directly. The measured
    per-frequency ADC L−R difference becomes the interface correction.
    Step 2 (purpose='dut'): insert the DUT and subtract that correction. The result
    is relative DUT mismatch ΔG(L−R), and does not require absolute volt calibration.

    riaa_compensate: уровень генератора каждой частоты смещается по обратной RIAA
    (для фонокорректоров: на входе АЦП уровень одинаковый на всех частотах).
    Цифровой запас ЦАП проверяется; при переполнении — отказ с подсказкой.
    frequency_profile: имя профиля из FREQUENCY_PROFILES (метаданные).
    """
    freqs = tuple(float(f) for f in frequencies)
    if not freqs or len(freqs) < 2:
        raise ValueError("для серии Gain Match нужны минимум две частоты")
    if any(not math.isfinite(f) or f <= 0 or f >= rig.fs / 2 for f in freqs):
        raise ValueError("частоты должны быть положительными и ниже Найквиста")
    if len({round(f, 9) for f in freqs}) != len(freqs):
        raise ValueError("частоты в серии не должны повторяться")
    if purpose not in ("loopback", "dut"):
        raise ValueError("purpose должен быть loopback или dut")
    level_dbfs = float(level_dbfs)
    if not math.isfinite(level_dbfs) or not -80.0 <= level_dbfs <= -0.5:
        raise ValueError("уровень генератора должен быть в диапазоне −80…−0.5 dBFS")
    repeats = int(repeats)
    if not 1 <= repeats <= 10:
        raise ValueError("число повторов должно быть от 1 до 10")
    if frequency_profile is not None and frequency_profile not in FREQUENCY_PROFILES:
        raise ValueError("неизвестный профиль частот: "
                         f"{frequency_profile!r}; доступны: {sorted(FREQUENCY_PROFILES)}")

    # Уровни генератора по частотам (RIAА-компенсация) и цифровой запас ЦАП.
    if riaa_compensate:
        generator_levels = {round(f, 6): level_dbfs + riaa_level_adjust_db(f) for f in freqs}
        peak_freq = max(generator_levels, key=lambda f: generator_levels[f])
        peak_lv = generator_levels[peak_freq]
        # Единый порог: максимуму генератора — запас 1 дБ до полной шкалы ЦАП.
        # Подсказка гарантирует, что повтор с предложенной базой пройдёт ту же
        # проверку (раньше двухступенчатая проверка давала невыполнимый совет).
        limit_db = -1.0
        if peak_lv > limit_db + 1e-9:      # epsilon: граница на float не должна ловить
            overflow_db = peak_lv - limit_db
            min_base = level_dbfs - overflow_db - 0.01   # 0.01 дБ запаса в подсказке
            raise ValueError(
                f"RIAА-компенсация: уровень генератора на {peak_freq:g} Гц "
                f"({peak_lv:+.2f} dBFS) превышает допустимые {limit_db:+.1f} dBFS "
                f"(запас 1 дБ до полной шкалы ЦАП); уменьши базовый уровень минимум "
                f"на {overflow_db + 0.01:.2f} дБ (до {min_base:+.2f} dBFS)")
    else:
        generator_levels = {round(f, 6): level_dbfs for f in freqs}

    backend = rig.be
    hw = getattr(backend, "hw", "sim")
    device_key = device_key_from_hw(hw)
    mixer_fingerprint = mixer_fingerprint_from_hw(hw)

    refs = None
    if purpose == "dut":
        if reference is None:
            raise ValueError("нет эталона петли карты: сначала выполни шаг 1")
        refs = validate_loopback_reference(rig, reference, level_dbfs, freqs,
                                           riaa_compensate=riaa_compensate)
    elif reference is not None:
        raise ValueError("при измерении петли эталон не нужен")

    tones = {}
    segments = []
    order = []
    for rep in range(repeats):
        for requested_f in freqs:
            k, amplitude = rig.amp_tone(requested_f, generator_levels[round(requested_f, 6)])
            actual_f = float(rig.f_of(k))
            tones[requested_f] = (k, actual_f)
            segments.append(([(k, amplitude)], [(k, amplitude)], None))
            order.append((requested_f, rep + 1, actual_f, k))

    windows, warnings = rig.run(segments)
    analyzer_info = []   # предупреждения анализатора собираются с контекстом
    per_frequency = {f: {"L": [], "R": []} for f in freqs}
    for order_item, window in zip(order, windows):
        requested_f, rep, actual_f, k = order_item
        for ch_index, channel in enumerate(("L", "R")):
            signal = np.asarray(window[:, ch_index], dtype=np.float64)
            analysis = rig._tone_an(window, ch_index, k, nharm=9)
            measured_f = float(analysis["f0"])
            level = float(analysis["level_dbfs"])
            snr = max(0.0, -float(analysis["thdn_db"]))
            residual_dbfs = level - snr
            noise_dbfs = float(analysis["noise_dbfs"])
            carrier_residual_db = calc_carrier_residual_db(level, analysis)
            peak = float(np.max(np.abs(signal))) if signal.size else 0.0
            clipped = peak >= 0.98
            stab = envelope_stability(signal, rig.fs, actual_f)
            reasons = []
            if not math.isfinite(level) or level <= -120.0:
                reasons.append("tone_missing")
            if carrier_residual_db < CARRIER_RESIDUAL_MIN_DB:
                reasons.append("low_carrier_residual")
            if clipped:
                reasons.append("adc_clip")
            if stab["trend_span"] > TREND_MAX or stab["z_burst"] > Z_BURST_MAX:
                reasons.append("unstable_carrier")
            absolute_gain = rig.cal.gain_db(generator_levels[round(requested_f, 6)], level, ch_index)
            input_v = rig.cal.dut_in_v(generator_levels[round(requested_f, 6)], ch_index)
            output_v = rig.cal.in_v(level, ch_index)
            for w in analysis.get("warnings", []):
                analyzer_info.append(f"{channel} {actual_f:.0f} Гц: {w}")
            per_frequency[requested_f][channel].append({
                "repeat": rep,
                "frequency_hz": measured_f,
                "level_dbfs": level,
                "generator_level_dbfs": generator_levels[round(requested_f, 6)],
                "residual_dbfs": residual_dbfs,
                "sinad_db": snr,
                "snr_db": snr,                      # устаревшее имя (совместимость JSON)
                "carrier_residual_db": carrier_residual_db,
                "noise_dbfs": noise_dbfs,
                "hum": list(analysis.get("hum", [])),
                "phase_coherence": analysis.get("phase_coherence"),
                "analyzer_warnings": [str(w) for w in analysis.get("warnings", [])],
                "envelope": stab,
                "peak_dbfs": to_db(peak),
                "clipped": clipped,
                "valid": not reasons,
                "validity_reasons": reasons,
                "absolute_gain_db": None if absolute_gain is None else float(absolute_gain),
                "input_vrms": input_v,
                "output_vrms": output_v,
            })

    # события формируются после сбора всех источников: аппаратные (fatal по
    # шаблону), анализаторные и частотные (info) — всё доходит до пользователя
    proximity = mains_proximity_warnings(
        freqs, float(rig.cal.get("mains", 50.0) or 50.0), df_hz=float(rig.df or 1.0))
    warnings = list(warnings) + proximity
    events = classify_warnings(warnings)
    events["info"] = list(events["info"]) + analyzer_info

    if events["fatal"]:
        # Фатальные аппаратные события (XRUN, перегрузка) инвалидируют ВСЮ серию:
        # привязки событий к отдельным сегментам пока нет (0.1.19).
        for f in per_frequency:
            for c in ("L", "R"):
                for q in per_frequency[f][c]:
                    if "hardware_fault" not in q["validity_reasons"]:
                        q["validity_reasons"] = sorted(q["validity_reasons"] + ["hardware_fault"])
                        q["valid"] = False

    assessed = repeats >= MIN_REPEATS_FOR_REFERENCE
    rows = []
    for requested_f in freqs:
        channels = {}
        for channel in ("L", "R"):
            samples = per_frequency[requested_f][channel]
            levels = [q["level_dbfs"] for q in samples]
            snrs = [q["snr_db"] for q in samples]
            channels[channel] = {
                "level_dbfs": float(np.median(levels)),
                "level_spread_db": float(max(levels) - min(levels)),
                "residual_dbfs": float(np.median([q["residual_dbfs"] for q in samples])),
                "sinad_db": float(np.median(snrs)),
                "snr_db": float(np.median(snrs)),   # устаревшее имя (совместимость)
                "carrier_residual_db": float(np.median([q["carrier_residual_db"] for q in samples])),
                "noise_dbfs": float(np.median([q["noise_dbfs"] for q in samples])),
                "absolute_gain_db": (float(np.median([q["absolute_gain_db"] for q in samples]))
                                     if all(q["absolute_gain_db"] is not None for q in samples) else None),
                "valid": all(q["valid"] for q in samples),
                "validity_reasons": sorted({reason for q in samples for reason in q["validity_reasons"]}),
                "repeats": samples,
            }
        delta_adc = channels["L"]["level_dbfs"] - channels["R"]["level_dbfs"]
        per_repeat_delta = [l["level_dbfs"] - r["level_dbfs"]
                            for l, r in zip(per_frequency[requested_f]["L"], per_frequency[requested_f]["R"])]
        if assessed:
            spread = float(max(per_repeat_delta) - min(per_repeat_delta))
            repeat_stable = bool(spread <= 0.10)
        else:
            spread = 0.0
            repeat_stable = None            # повторяемость не оценена
        ref_row = refs.get(round(requested_f, 6)) if refs else None
        correction = float(ref_row["delta_adc_db"]) if ref_row else None
        corrected = (delta_adc - correction) if purpose == "dut" else None
        rows.append({
            "requested_frequency_hz": requested_f,
            "frequency_hz": tones[requested_f][1],
            "channels": channels,
            "delta_adc_db": float(delta_adc),
            "loopback_correction_db": correction,
            "delta_gain_db": None if corrected is None else float(corrected),
            "delta_output_spread_db": spread,
            "repeat_stable": repeat_stable,
            "valid": channels["L"]["valid"] and channels["R"]["valid"],
        })

    result = {
        "kind": "gain_match_series",
        "schema": 3,
        "purpose": purpose,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device": _device_description(backend),
        "device_key": device_key,
        "mixer_fingerprint": mixer_fingerprint,
        "backend": getattr(backend, "name", type(backend).__name__),
        "fs": int(rig.fs),
        "generator_level_dbfs": level_dbfs,
        "generator_levels_dbfs": generator_levels,
        "riaa_compensated": bool(riaa_compensate),
        "frequency_profile": frequency_profile,
        "frequencies_hz": [row["frequency_hz"] for row in rows],
        "repeats": repeats,
        "repeatability": "assessed" if assessed else "not_assessed",
        "blocks": int(rig.blocks),
        "settle_seconds": float(rig.settle),
        "events": events,
        "warnings": list(warnings),
        "absolute_voltage_calibrated": all(
            row["channels"]["L"]["absolute_gain_db"] is not None
            and row["channels"]["R"]["absolute_gain_db"] is not None for row in rows),
        "loopback_corrected": purpose == "dut",
        "reference_created": reference.get("created") if reference else None,
        "rows": rows,
        "metadata": dict(metadata or {}),
    }
    return result


def text_series(result, tolerance_db=DEFAULT_MATCH_TOL_DB):
    """Short user-facing result; hides raw dBFS internals unless measurement failed."""
    rows = result.get("rows", [])
    events = result.get("events") or {}
    if result.get("purpose") == "loopback":
        lines = ["ШАГ 1 ИЗ 2 · ЭТАЛОН ЗВУКОВОЙ КАРТЫ",
                 "Петля L→L и R→R проверена. Измерена собственная разница каналов карты:"]
        for row in rows:
            lines.append(f"{row['frequency_hz']:g} Гц: поправка {row['delta_adc_db']:+.3f} дБ")
        lines += ["Эталон сохранён. Теперь подключи проверяемый усилитель, не меняя карту, "
                  "уровень генератора и аттенюаторы."]
        if result.get("backend") != "sim" and not result.get("mixer_fingerprint"):
            lines.append("ПРЕДУПРЕЖДЕНИЕ: снимок ALSA-микшера не получен; его изменения между шагами автоматически не обнаружатся.")
        if not all(row["valid"] and row.get("repeat_stable") is True for row in rows):
            lines.append("ВНИМАНИЕ: петля нестабильна или измерена ненадёжно — эталон нельзя применять.")
        if events.get("fatal"):
            lines.append("ФАТАЛЬНЫЕ СОБЫТИЯ: " + "; ".join(events["fatal"][:4]))
        return "\n".join(lines)

    lines = ["ШАГ 2 ИЗ 2 · РЕЗУЛЬТАТ УСИЛИТЕЛЯ",
             f"Сравнение каналов после компенсации звуковой карты · допуск ±{tolerance_db:.2f} дБ"]
    if result.get("riaa_compensated"):
        lines.append("Режим RIAA-компенсации, фактические уровни генератора: " +
                     ", ".join(f"{f:g} Гц {lv:+.2f} dBFS"
                               for f, lv in sorted(result.get("generator_levels_dbfs", {}).items())))
    assessed = result.get("repeatability", "assessed") == "assessed"
    values = []
    all_valid = True
    all_stable = True
    for row in rows:
        delta = row["delta_gain_db"]
        valid = row["valid"] and delta is not None
        stable = row.get("repeat_stable")
        all_valid &= valid
        all_stable &= (stable is True) if assessed else (stable is None)
        if not valid:
            verdict = "НЕТ ДОСТОВЕРНОГО ИЗМЕРЕНИЯ"
        elif assessed and stable is not True:
            verdict = "НЕСТАБИЛЬНО — повторить"
        elif not assessed:
            verdict = "измерено (повторяемость не оценена)"
        elif abs(delta) <= tolerance_db:
            verdict = "СОВПАДАЮТ"
        else:
            stronger = "левый" if delta > 0 else "правый"
            verdict = f"{stronger.capitalize()} сильнее на {abs(delta):.3f} дБ"
        lines.append(f"{row['frequency_hz']:g} Гц:  ΔG L−R = "
                     f"{'—' if delta is None else f'{delta:+.3f} дБ'}  ·  {verdict}")
        if assessed:
            lines.append(f"       повторяемость: {row['delta_output_spread_db']:.3f} дБ")
        else:
            lines.append("       повторяемость: не оценена (один повтор)")
        reasons = sorted({r for c in ("L", "R") for r in row["channels"][c]["validity_reasons"]})
        if reasons:
            lines.append(f"       причины: {', '.join(reasons)}")
        if valid:
            values.append(abs(delta))
    if not assessed:
        lines.append("ИТОГ: ПРЕДВАРИТЕЛЬНЫЙ РЕЗУЛЬТАТ — повторяемость не оценена (один повтор); "
                     "для заключения повтори с 2+ повторами")
    elif not all_valid:
        lines.append("ИТОГ: ИЗМЕРЕНИЕ НЕ ЗАСЧИТАНО — проверь уровни, кабели и перегрузку АЦП.")
    elif not all_stable:
        lines.append("ИТОГ: РЕЗУЛЬТАТ НЕСТАБИЛЕН — сначала устрани разброс повторов.")
    else:
        worst = max(values, default=0.0)
        if worst <= tolerance_db:
            lines.append(f"ИТОГ: КАНАЛЫ СОГЛАСОВАНЫ · максимальная разница {worst:.3f} дБ.")
        else:
            lines.append(f"ИТОГ: КАНАЛЫ НЕ СОГЛАСОВАНЫ · максимальная разница {worst:.3f} дБ.")
    if events.get("fatal"):
        lines.append("ФАТАЛЬНЫЕ АППАРАТНЫЕ СОБЫТИЯ (результат недостоверен): " +
                     "; ".join(events["fatal"][:4]))
    if events.get("info"):
        lines.append("Замечания анализатора/стенда: " + "; ".join(events["info"][:4]))
    lines.append("ΔG — относительная разница усиления L/R; абсолютная калибровка вольт для неё не требуется.")
    if result.get("metadata"):
        meta = result["metadata"]
        if meta.get("dut"):
            lines.append(f"Устройство: {meta['dut']}")
        if meta.get("tubes"):
            lines.append(f"Лампы/позиции: {meta['tubes']}")
        if meta.get("notes"):
            lines.append(f"Примечание: {meta['notes']}")
    return "\n".join(lines)
