"""АЧХ по тестовой пластинке: накопление пиков и сохранённые кривые (0.1.20).

Идея. Пластинка проигрывает свип-тон (или шум/набор тонов); анализатор в реальном времени
запоминает максимум уровня в каждой частотной полосе и удерживает его до сброса — итоговая
огибающая и есть АЧХ тракта «картридж + фонокорректор». Несколько измерений сохраняются как
кривые разных цветов (другой картридж, другой фонокорректор) и сравниваются на одном
графике; переходное затухание (channel separation) — разность уровня в «чужом» и в ведущем
канале на той же частоте.

Два способа накопления, оба считаются одновременно:

* **tone** (трекинг тона, рекомендуется) — в каждом кадре находится доминирующий тон; уровень
  обоих каналов берётся в полосе ±1/12 октавы вокруг него и заносится в ячейку на частоте
  тона. Щелчки пластинки, рокот и шум дорожки в накопление не попадают: их частота не совпадает
  с тоном свипа. Для переходного затухания «чужой» канал читается именно на частоте тона.
* **bands** (RTA peak hold) — максимум мощности в долях октавы по всему спектру, как в обычном
  анализаторе. Подходит для шума и набора тонов, но запоминает и щелчки.

Модуль без GTK: функция ``rta_job`` выполняется в процессе анализа (``AnalysisWorker``).
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

import numpy as np

from . import dsp

PALETTE = ((0.90, 0.22, 0.22), (0.86, 0.30, 0.86), (0.25, 0.72, 0.38), (0.15, 0.74, 0.80),
           (0.92, 0.80, 0.15), (0.60, 0.40, 0.95), (0.95, 0.60, 0.20), (0.75, 0.75, 0.75))
TONE_PROMINENCE_DB = 25.0
PER_OCTAVE_CHOICES = (3, 6, 12, 24)


def band_edges(per_octave=12, fmin=20.0, fmax=20000.0):
    """Границы полос 1/N октавы от fmin до не менее чем fmax."""
    nb = int(math.ceil(math.log2(fmax / fmin) * per_octave - 1e-9))
    return fmin * 2.0 ** (np.arange(nb + 1) / float(per_octave))


def band_centers(edges):
    return np.sqrt(edges[:-1] * edges[1:])


def lf_limit_hz(fs, n):
    """Частота, ниже которой окно слишком короткое: тон ближе ~6 бинов к нулю сливается со своим
    зеркальным отражением и со стоком постоянной составляющей — оценка недостоверна."""
    return 6.0 * float(fs) / float(n)


def window_samples(fs, seconds):
    """Размер FFT-окна (степень двойки) для заданной длительности — окно задаётся временем, а не
    числом отсчётов: одинаковая точность на НЧ при 96 и 192 кГц."""
    return int(2 ** round(math.log2(max(1024.0, float(seconds) * float(fs)))))


def band_levels_db(freq, power, edges):
    """Уровень в полосах, dBFS (0 dB = синус во всю шкалу). power — (nbins,) или (ch, nbins) RMS²/бин.

    Полоса без единого бина (узкие НЧ-полосы при коротком окне) — NaN, а не ноль."""
    power = np.atleast_2d(np.asarray(power, dtype=np.float64))
    lo = np.searchsorted(freq, edges[:-1], side="left")
    hi = np.searchsorted(freq, edges[1:], side="left")
    cs = np.concatenate((np.zeros((power.shape[0], 1)), np.cumsum(power, axis=1)), axis=1)
    p = cs[:, hi] - cs[:, lo]
    out = dsp.rms_dbfs(np.sqrt(np.maximum(p, 1e-30)))
    out[:, hi <= lo] = np.nan
    return out


def detect_tone(freq, power2, f_lo=20.0, f_hi=20000.0, prominence_db=TONE_PROMINENCE_DB):
    """Доминирующий тон кадра: (f0, k0) или None. power2 — суммарная мощность каналов."""
    a = int(np.searchsorted(freq, f_lo))
    b = int(np.searchsorted(freq, f_hi))
    if b - a < 16:
        return None
    seg = power2[a:b]
    k = int(np.argmax(seg))
    if seg[k] < 10.0 ** (prominence_db / 10.0) * float(np.median(seg)):
        return None
    k += a
    lo, hi = max(0, k - 3), min(power2.size, k + 4)
    w = power2[lo:hi]
    return float(np.sum(freq[lo:hi] * w) / np.sum(w)), k


def tone_band_bins(freq, power_sum, f0):
    """Диапазон бинов [lo, hi), в котором лежит энергия тона f0 (в том числе размазанного свипом).

    Не уже ±1/12 октавы И не уже ±5 бинов: главный лепесток окна Блэкмана–Харриса — ±4 бина.
    На 20–40 Гц при коротком окне ±1/12 октавы (±1.2…2.4 Гц) меньше одного бина, и без этого
    уровень занижался до −2.4 дБ (192 кГц, окно 0.34 с). Дополнительно полоса расширяется по
    «следу» свипа — соседним бинам не слабее −30 дБ от пика, но не дальше ±1/6 октавы: при
    быстром свипе и длинном окне тон успевает пройти заметный участок частот."""
    df = float(freq[1] - freq[0])
    bw = 2.0 ** (1.0 / 12.0)
    lo = int(np.searchsorted(freq, min(f0 / bw, f0 - 5.0 * df)))
    hi = max(int(np.searchsorted(freq, max(f0 * bw, f0 + 5.0 * df))), lo + 1)
    lo = max(lo, 0)
    hi = min(hi, power_sum.size)
    k = lo + int(np.argmax(power_sum[lo:hi]))
    thr = float(power_sum[k]) * 1e-3
    lim_lo = int(np.searchsorted(freq, f0 / 2.0 ** (1.0 / 6.0)))
    lim_hi = int(np.searchsorted(freq, f0 * 2.0 ** (1.0 / 6.0)))
    a_ = lo
    while a_ > lim_lo and a_ > 0 and power_sum[a_ - 1] >= thr:
        a_ -= 1
    b_ = hi
    while b_ < lim_hi and b_ < power_sum.size and power_sum[b_] >= thr:
        b_ += 1
    return max(0, a_ - 5 if a_ < lo else lo), min(power_sum.size, b_ + 5 if b_ > hi else hi)


def tone_levels_db(freq, power, f0):
    """Уровень каналов (dBFS) в полосе тона — см. tone_band_bins."""
    power = np.atleast_2d(power)
    lo, hi = tone_band_bins(freq, power.sum(axis=0), f0)
    p = np.sum(power[:, lo:hi], axis=1)
    return dsp.rms_dbfs(np.sqrt(np.maximum(p, 1e-30)))


def coherent_sums(Xd, Xo, power_d, lo, hi, footprint_db=30.0):
    """(числитель, знаменатель) оценки передаточного коэффициента H = Σ conj(Xd)·Xo / Σ|Xd|².

    Суммирование идёт по «следу» тона (бины ведущего канала не слабее пика на footprint_db).
    Числитель и знаменатель копятся по ячейкам частоты ОТДЕЛЬНО по кадрам: когерентное среднее
    подавляет некоррелированные с тоном шум дорожки, рокот и щелчки (дальше растёт с числом
    кадров), а максимум зашумлённых оценок завышал бы слабый канал. Предел один: часть
    рокота, общая для каналов, от утечки неотличима."""
    pd = power_d[lo:hi]
    if pd.size == 0:
        return None
    m = pd >= float(np.max(pd)) * 10.0 ** (-footprint_db / 10.0)
    if not m.any():
        return None
    xd, xo = Xd[lo:hi][m], Xo[lo:hi][m]
    den = float(np.sum(np.abs(xd) ** 2))
    if den <= 0:
        return None
    return complex(np.sum(np.conj(xd) * xo)), den


def rta_job(x, fs, n, per_octave=12, fmin=20.0, fmax=20000.0):
    """Кадр анализа для страницы «АЧХ по пластинке» (выполняется в дочернем процессе).

    Возвращает {'bands': (2, nb) dBFS, 'tone': {'f','lvl'(2,)}|None, 'level_db': (2,)} — тон ищется
    по сумме каналов; 'level_db' — общий RMS кадра (dBFS) для индикации «есть сигнал»."""
    t0 = time.perf_counter()
    x = np.asarray(x, dtype=np.float64)[-n:]
    w = dsp.bh(n)
    X = [np.fft.rfft(x[:, c] * w) for c in (0, 1)]
    scale = 2.0 / (n * float(np.sum(w * w)))
    P = np.stack([scale * np.abs(X[c]) ** 2 for c in (0, 1)])
    P[:, 0] *= 0.5
    if n % 2 == 0:
        P[:, -1] *= 0.5
    freq = np.arange(P.shape[1]) * (fs / n)
    edges = band_edges(per_octave, fmin, fmax)
    out = {"n": int(n), "fs": float(fs), "df": float(fs / n), "lf_limit_hz": lf_limit_hz(fs, n),
           "edges": edges, "bands": band_levels_db(freq, P, edges),
           "tone": None, "level_db": [float(dsp.rms_dbfs(np.sqrt(np.mean(x[:, c] ** 2)))) for c in (0, 1)]}
    tn = detect_tone(freq, P[0] + P[1], fmin, fmax)
    if tn is not None:
        f0 = tn[0]
        lvl = tone_levels_db(freq, P, f0)
        d = 0 if lvl[0] >= lvl[1] else 1
        lo, hi = tone_band_bins(freq, P[0] + P[1], f0)
        cs = coherent_sums(X[d], X[1 - d], P[d], lo, hi)
        out["tone"] = {"f": f0, "lvl": np.array(lvl, dtype=float), "driven": d,
                       "num": cs[0] if cs else None, "den": cs[1] if cs else None}
    out["elapsed"] = time.perf_counter() - t0
    return out


class PeakHoldAccumulator:
    """Удержание пиков по сетке 1/N октавы (два режима накопления одновременно).

    Режим bands — максимум мощности полосы. Режим tone — по ячейке: пик уровня ведущего канала
    (то, что и просил пользователь: пик удерживается до сброса) и когерентная сумма для чужого
    канала (см. coherent_sums); уровень чужого канала = пик ведущего + 20·lg|H|."""

    def __init__(self, per_octave=12, fmin=20.0, fmax=20000.0):
        self.per_octave, self.fmin, self.fmax = int(per_octave), float(fmin), float(fmax)
        self.edges = band_edges(self.per_octave, self.fmin, self.fmax)
        self.centers = band_centers(self.edges)
        self.reset()

    def reset(self):
        nb = self.centers.size
        self.bands = np.full((2, nb), np.nan)          # режим «полосы»
        self.own = np.full((2, nb), np.nan)            # режим tone: пик собственного уровня канала
        self.drv_level = np.full(nb, np.nan)           # пик уровня ведущего канала
        self.drv_idx = np.full(nb, -1, dtype=int)
        self.num = np.zeros(nb, dtype=complex)
        self.den = np.zeros(nb)
        self.count = np.zeros(nb, dtype=int)           # кадров тона в ячейке
        self.frames = 0
        self.tone_frames = 0
        self.last_tone = None

    @property
    def hold(self):
        """Совместимый вид: {'tone': (2, nb), 'bands': (2, nb)}."""
        return {"tone": self.tone_hold(), "bands": self.bands}

    def band_index(self, f):
        i = int(math.floor(math.log2(f / self.fmin) * self.per_octave))
        return i if 0 <= i < self.centers.size else None

    def update(self, bands_db, tone):
        """bands_db (2, nb) или None; tone {'f','lvl','driven','num','den'} или None."""
        self.frames += 1
        if bands_db is not None and bands_db.shape == self.bands.shape:
            self.bands = np.fmax(self.bands, bands_db)
        if tone is None:
            return
        i = self.band_index(tone["f"])
        if i is None:
            return
        lvl = np.asarray(tone["lvl"], dtype=float)
        d = int(tone.get("driven", 0 if lvl[0] >= lvl[1] else 1))
        self.own[:, i] = np.fmax(self.own[:, i], lvl)
        if not (self.drv_level[i] >= lvl[d]):
            self.drv_level[i] = lvl[d]
            self.drv_idx[i] = d
        if tone.get("num") is not None and tone.get("den"):
            self.num[i] += tone["num"]
            self.den[i] += tone["den"]
        self.count[i] += 1
        self.tone_frames += 1
        self.last_tone = tone["f"]

    def tone_hold(self):
        """(2, nb) dBFS: ведущий канал — пик; чужой — когерентная утечка (не больше собственного пика)."""
        out = self.own.copy()
        have = self.den > 0
        for i in np.flatnonzero(have & np.isfinite(self.drv_level)):
            d = int(self.drv_idx[i])
            if d < 0:
                continue
            out[d, i] = self.drv_level[i]
            leak = self.drv_level[i] + 20.0 * math.log10(max(abs(self.num[i]) / self.den[i], 1e-12))
            out[1 - d, i] = min(self.own[1 - d, i], leak)
        return out

    def coverage(self):
        """(охвачено ячеек, всего) — прогресс свипа в режиме трекинга тона."""
        return int(np.count_nonzero(self.count)), int(self.count.size)

    def snapshot(self, name, color, mode, fs, card="", mixer="", corr_db=None, corr_note=""):
        levels = self.tone_hold() if mode == "tone" else self.bands.copy()
        return Overlay(name=name, color=tuple(color), mode=mode, per_octave=self.per_octave,
                       fmin=self.fmin, fmax=self.fmax, fs=float(fs), centers=self.centers.copy(),
                       levels=levels, card=card, mixer=mixer,
                       corr_db=None if corr_db is None else np.array(corr_db, dtype=float),
                       corr_note=corr_note)


def correction_curve(profile, centers):
    """Поправка АЧХ карты (дБ) для центров полос: (2, nb) или None.

    Профиль — калибровка петли ЦАП→АЦП (`measure.build_response_calibration`), нормированная на
    1 кГц. Для записи с пластинки нужен только вход, но отдельного профиля АЦП нет: петля
    включает и ЦАП (у современных карт его вклад мал, но не нулевой — об этом пишет страница).
    Вне калиброванного диапазона поправка нулевая (без экстраполяции), как в apply_response_calibration."""
    if not isinstance(profile, dict):
        return None
    out = np.zeros((2, centers.size))
    ok = False
    for c, name in enumerate(("L", "R")):
        p = (profile.get("channels") or {}).get(name)
        if not p:
            continue
        pf = np.asarray(p.get("frequency_hz", []), dtype=float)
        pfr = np.asarray(p.get("fr_norm_db", []), dtype=float)
        if pf.size < 2 or pfr.size != pf.size:
            continue
        lf = np.log10(pf)
        valid = (centers >= pf[0]) & (centers <= pf[-1])
        if valid.any():
            out[c, valid] = np.interp(np.log10(centers[valid]), lf, pfr) - float(np.interp(3.0, lf, pfr))
            ok = True
    return out if ok else None


def split_driven(levels):
    """(индекс ведущего канала, переходное затухание other−driven, дБ) по уровням (2, nb) с NaN."""
    both = np.isfinite(levels[0]) & np.isfinite(levels[1])
    if not both.any():
        return 0, np.full(levels.shape[1], np.nan)
    med = [float(np.median(levels[c][both])) for c in (0, 1)]
    d = 0 if med[0] >= med[1] else 1
    return d, levels[1 - d] - levels[d]


def relative_levels(centers, levels, ref_hz=1000.0, leak_db=10.0):
    """Нормировка на уровень в полосе вокруг ref_hz.

    Оба канала ведущие (стерео-свип): каждый на свой ноль. Один канал — утечка (медианы каналов
    различаются больше leak_db): общий ноль по ведущему, чтобы переходное затухание осталось
    видимым отступом (L = 0 дБ, R = −25 дБ), а не «выровнялось» к нулю."""
    i = int(np.argmin(np.abs(np.log(centers / ref_hz))))

    def ref_of(row):
        if np.isfinite(row[i]):
            return float(row[i])
        nb = row[max(0, i - 1): i + 2]
        nb = nb[np.isfinite(nb)]
        return float(np.mean(nb)) if nb.size else None
    d, _xt = split_driven(levels)
    both = np.isfinite(levels[0]) & np.isfinite(levels[1])
    leak = bool(both.any()) and abs(float(np.median(levels[0][both]) - np.median(levels[1][both]))) > leak_db
    refs = [ref_of(levels[0]), ref_of(levels[1])]
    if leak and refs[d] is not None:
        refs = [refs[d], refs[d]]
    out = levels.copy()
    for c in range(levels.shape[0]):
        if refs[c] is not None:
            out[c] = levels[c] - refs[c]
    return out


@dataclass
class Overlay:
    name: str
    color: tuple
    mode: str
    per_octave: int
    fmin: float
    fmax: float
    fs: float
    centers: np.ndarray
    levels: np.ndarray                  # (2, nb) dBFS, NaN — нет данных
    visible: bool = True
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    # Измерительный контекст: сравнивать кривые из разных контекстов можно только условно
    card: str = ""                     # устойчивый идентификатор карты
    mixer: str = ""                    # короткий отпечаток настроек ALSA-микшера
    corr_db: object = None             # (2, nb) поправка петли карты на момент записи или None
    corr_note: str = ""

    def to_json(self):
        return {"name": self.name, "color": list(self.color), "mode": self.mode,
                "per_octave": self.per_octave, "fmin": self.fmin, "fmax": self.fmax, "fs": self.fs,
                "centers": [round(float(v), 4) for v in self.centers],
                "levels": [[None if not math.isfinite(v) else round(float(v), 3) for v in row]
                           for row in self.levels],
                "visible": self.visible, "created": self.created,
                "card": self.card, "mixer": self.mixer, "corr_note": self.corr_note,
                "corr_db": None if self.corr_db is None else
                [[round(float(v), 3) for v in row] for row in np.asarray(self.corr_db)]}

    @staticmethod
    def from_json(d):
        lv = np.array([[np.nan if v is None else float(v) for v in row] for row in d["levels"]])
        return Overlay(name=str(d["name"]), color=tuple(float(c) for c in d["color"]), mode=d["mode"],
                       per_octave=int(d["per_octave"]), fmin=float(d["fmin"]), fmax=float(d["fmax"]),
                       fs=float(d.get("fs", 0.0)), centers=np.array(d["centers"], dtype=float),
                       levels=lv, visible=bool(d.get("visible", True)), created=str(d.get("created", "")),
                       card=str(d.get("card", "")), mixer=str(d.get("mixer", "")),
                       corr_db=(None if d.get("corr_db") is None else np.array(d["corr_db"], dtype=float)),
                       corr_note=str(d.get("corr_note", "")))


def overlays_to_json(overlays):
    return {"version": 1, "overlays": [o.to_json() for o in overlays]}


def overlays_from_json(data):
    out = []
    for d in (data or {}).get("overlays", []):
        try:
            out.append(Overlay.from_json(d))
        except (KeyError, TypeError, ValueError):
            continue          # повреждённую запись пропускаем, остальные не теряем
    return out


def overlays_csv(overlays, normalize=False, corrected=False):
    """CSV: частота и по каждой кривой L, R (dB) и переходное затухание.

    corrected — вычесть поправку петли карты, записанную с кривой (если её не было — сырая, это
    видно в строке-комментарии). Строки «#» перед заголовком — метаданные измерительного контекста."""
    if not overlays:
        return ""
    meta = []
    cols = ["freq_hz"]
    data = [overlays[0].centers]
    for o in overlays:
        applied = bool(corrected and o.corr_db is not None)
        meta.append(f"# {o.name}: card={o.card or '?'} fs={o.fs:.0f} mixer={o.mixer or '?'} mode={o.mode} "
                    f"1/{o.per_octave}oct created={o.created} correction={'applied' if applied else 'raw'}"
                    + (f" ({o.corr_note})" if o.corr_note else ""))
        base = o.levels - np.asarray(o.corr_db) if applied else o.levels
        lv = relative_levels(o.centers, base) if normalize else base
        d, xt = split_driven(o.levels)
        for c, ch in enumerate("LR"):
            cols.append(f"{o.name} {ch}")
            data.append(_resample(o, lv[c], overlays[0].centers))
        cols.append(f"{o.name} xtalk(other-driven)")
        data.append(_resample(o, xt, overlays[0].centers))
    lines = meta + [",".join('"' + c.replace('"', "'") + '"' for c in cols)]
    for i in range(len(data[0])):
        lines.append(",".join("" if (j > 0 and not math.isfinite(col[i])) else f"{col[i]:.4f}"
                              for j, col in enumerate(data)))
    return "\n".join(lines) + "\n"


def _resample(o, y, centers):
    """Кривая другой сетки — на сетку первой (интерполяция по log f; вне диапазона — NaN)."""
    if o.centers.size == centers.size and np.allclose(o.centers, centers):
        return np.asarray(y, dtype=float)
    ok = np.isfinite(y)
    if ok.sum() < 2:
        return np.full(centers.size, np.nan)
    return np.interp(np.log(centers), np.log(o.centers[ok]), y[ok], left=np.nan, right=np.nan)
