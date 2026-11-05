"""Анализ записей тестовых пластинок (в т.ч. TACET Vinyl: Check).

Функции не предполагают, что каждая дорожка — калиброванный лабораторный сигнал.
Результаты сопровождаются описанием ограничений методики.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from scipy import signal as sg
from scipy.io import wavfile

from . import dsp


def read_wav(path):
    fs, raw = wavfile.read(str(path))
    if raw.ndim == 1:
        raw = raw[:, None]
    if np.issubdtype(raw.dtype, np.integer):
        info = np.iinfo(raw.dtype)
        scale = float(max(abs(info.min), abs(info.max)))
        x = raw.astype(np.float64) / scale
    elif np.issubdtype(raw.dtype, np.floating):
        x = raw.astype(np.float64)
    else:
        raise ValueError(f"неподдерживаемый формат WAV: {raw.dtype}")
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return int(fs), x


def write_wav(path, fs, x):
    """Экспорт в float32 WAV без потери исходных значений float."""
    x = np.asarray(x, dtype=np.float32)
    wavfile.write(str(path), int(fs), x)


def _tone_rms(x, fs, f, half=5):
    blk = dsp.Block(x, fs)
    return math.sqrt(max(0.0, blk.lobe(f, half)))


def analyze_phase(x, fs, lo=30.0, hi=16000.0):
    """Анализ TACET A3: mono / mono-out-of-phase pink-noise фрагмент.

    Для широкополосного шума нельзя измерять только «фазу на 1 кГц»:
    результат зависит от случайной реализации шума. Вместо этого измеряются
    широкополосные RMS, нулевая корреляция и круговое среднее фазы cross-spectrum
    только в полосе, где межканальная когерентность достаточно высока.
    """
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        raise ValueError("для фазового теста нужна стереозапись")
    if x.shape[1] < 2 or len(x) < fs:
        raise ValueError("для фазового теста нужна стереозапись не короче 1 секунды")
    hi = min(float(hi), 0.45 * float(fs))
    lo = max(10.0, float(lo))
    if not 0 < lo < hi:
        raise ValueError("некорректная полоса фазового анализа")
    l0 = x[:, 0] - np.mean(x[:, 0])
    r0 = x[:, 1] - np.mean(x[:, 1])
    sos = sg.butter(4, [lo, hi], btype="bandpass", fs=fs, output="sos")
    l = sg.sosfiltfilt(sos, l0)
    r = sg.sosfiltfilt(sos, r0)
    amps = [float(np.sqrt(np.mean(z * z))) for z in (l, r)]
    corr = float(np.corrcoef(l, r)[0, 1]) if np.std(l) > 0 and np.std(r) > 0 else float("nan")
    nperseg = min(8192, len(l))
    ff, pxy = sg.csd(l, r, fs=fs, window="hann", nperseg=nperseg,
                     noverlap=nperseg // 2, detrend="constant", scaling="density")
    _, coh = sg.coherence(l, r, fs=fs, window="hann", nperseg=nperseg,
                          noverlap=nperseg // 2, detrend="constant")
    sel = (ff >= lo) & (ff <= hi) & (coh >= 0.5) & (np.abs(pxy) > 1e-24)
    if np.any(sel):
        # Circular mean, weighted by coherence; insensitive to ±180° wrap.
        weights = coh[sel]
        vector = np.sum(weights * np.exp(1j * np.angle(pxy[sel])))
        phase = float(np.degrees(np.angle(vector)))
        coh_median = float(np.median(coh[sel]))
        coherent_fraction = float(np.count_nonzero(sel) / max(1, np.count_nonzero((ff >= lo) & (ff <= hi))))
    else:
        phase, coh_median, coherent_fraction = float("nan"), 0.0, 0.0
    return {"kind": "phase", "band_low_hz": lo, "band_high_hz": hi,
            "left_rms": amps[0], "right_rms": amps[1],
            "level_delta_db": dsp.db20(amps[0] / max(amps[1], 1e-20)),
            "correlation": corr, "phase_r_minus_l_deg": phase,
            "median_coherence": coh_median, "coherent_fraction": coherent_fraction,
            "comment": "для mono ожидаются близкие RMS, корреляция около +1 и фаза около 0°; "
                       "для mono-out-of-phase — корреляция около −1 и фаза около ±180°. "
                       "Результат относится ко всей цепи воспроизведения."}


def analyze_3150_stability(x, fs, nominal=3150.0):
    """Частотная стабильность дорожки TACET A2.

    Возвращает нефильтрованную статистику девиации частоты. Это НЕ готовый
    стандартизованный DIN/IEC Wow & Flutter %, поскольку не реализованы
    соответствующие weighting-фильтры и взвешивающий детектор.
    """
    x = np.asarray(x, dtype=float).ravel()
    if len(x) < int(fs * 2):
        raise ValueError("нужно записать не менее 2 секунд тона 3150 Гц")
    lo, hi = nominal * 0.8, nominal * 1.2
    sos = sg.butter(4, [lo, hi], btype="bandpass", fs=fs, output="sos")
    y = sg.sosfiltfilt(sos, x)
    analytic = sg.hilbert(y)
    phase = np.unwrap(np.angle(analytic))
    inst = np.diff(phase) * fs / (2 * np.pi)
    edge = min(int(0.25 * fs), len(inst) // 10)
    inst = inst[edge:-edge] if edge else inst
    # Удаляем выбросы краёв/провалов огибающей.
    env = np.abs(analytic)[1:][edge:len(analytic)-1-edge] if edge else np.abs(analytic)[1:-1]
    good = env > max(float(np.percentile(env, 30)), 1e-10)
    inst = inst[:len(good)][good]
    if inst.size < fs:
        raise ValueError("тон 3150 Гц не обнаружен или слишком слабый")
    mean = float(np.mean(inst))
    dev = inst - mean
    # Низкочастотная/высокочастотная составляющие показаны отдельно, но без
    # нормативных weighting curves это только диагностические полосы.
    sos_wow = sg.butter(2, 6.0, btype="lowpass", fs=fs, output="sos")
    slow = sg.sosfiltfilt(sos_wow, dev)
    fast = dev - slow
    return {"kind": "wow_flutter_unweighted", "nominal_hz": nominal, "mean_hz": mean,
            "std_hz": float(np.std(dev)), "peak_dev_hz": float(np.max(np.abs(dev))),
            "std_ppm": float(np.std(dev) / nominal * 1e6),
            "slow_std_hz": float(np.std(slow)), "fast_std_hz": float(np.std(fast)),
            "note": "не стандартизованный Wow & Flutter: без weighting-кривой и калиброванного тестового диска.",
            "time_s": np.arange(len(inst)) / fs, "frequency_hz": inst}


def analyze_vta_sum(x, fs, f1=341.0, f2=659.0, fs_sum=1000.0):
    """Уровни 341/659 Гц и суммы 1000 Гц на TACET A5."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 2:
        x = np.mean(x[:, :2], axis=1)
    if len(x) < fs:
        raise ValueError("для анализа дорожки 341+659 Гц нужна запись от 1 секунды")
    # Удаление DC и небольшая защита от краевых переходов.
    x = x[int(0.1 * fs):int(len(x) - 0.1 * fs)]
    a1, a2, asum = (_tone_rms(x, fs, f) for f in (f1, f2, fs_sum))
    denom = max(a1 + a2, 1e-20)
    return {"kind": "vta_sum", "f1_hz": f1, "f2_hz": f2, "sum_hz": fs_sum,
            "f1_rms": a1, "f2_rms": a2, "sum_rms": asum,
            "sum_dbc_vs_mean_tone": dsp.db20(asum / max((a1 + a2) / 2, 1e-20)),
            "sum_percent_of_tone_sum": 100.0 * asum / denom,
            "note": "продукт 1000 Гц — индикатор нелинейного/геометрического эффекта, но не прямое измерение угла VTA; тракт и картридж тоже влияют."}


def analyze_skating_300(x, fs, tone=300.0, window_s=0.5, nharm=5):
    """Скользящий THD/уровень для TACET A6 (растущий 300 Гц, около 41 с)."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = np.column_stack((x, x))
    if x.shape[1] < 2:
        raise ValueError("нужна стереозапись")
    win = int(round(window_s * fs))
    hop = win // 2
    if win < int(fs * 0.2) or len(x) < win * 2:
        raise ValueError("запись слишком короткая для анализа растущего тона")
    rows = []
    for start in range(0, len(x) - win + 1, hop):
        row = {"time_s": (start + win / 2) / fs}
        for c, name in ((0, "L"), (1, "R")):
            a = dsp.analyze_tone(x[start:start+win, c], fs, tone, win, nharm=nharm,
                                 band=(20.0, min(20000.0, 0.45*fs)), mains=0)
            row[name + "_level_dbfs"] = a["level_dbfs"]
            row[name + "_thd_pct"] = a["thd_pct"]
        rows.append(row)
    return {"kind": "skating_300", "rows": rows,
            "note": "синхронное начало и характер искажений в каналах помогают сравнить срыв считывания; это не калиброванная сила skating."}


def analyze_arm_resonance(x, fs, lo=5.0, hi=15.0):
    """Спектр 5–15 Гц для дорожки A7; показывает возможную резонансную область."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 2:
        x = np.mean(x[:, :2], axis=1)
    if len(x) < int(fs * 4):
        raise ValueError("нужна запись не короче 4 секунд; для точного пика лучше 20–30 секунд")
    x = x - np.mean(x)
    nfft = max(8192, 1 << int(np.ceil(np.log2(len(x)))))
    f, p = sg.welch(x, fs=fs, window="hann", nperseg=min(65536, len(x)),
                    noverlap=min(32768, len(x)//2), nfft=max(65536, min(nfft, 262144)))
    sel = (f >= lo) & (f <= hi)
    if not np.any(sel):
        raise ValueError("полоса 5–15 Гц не попала в спектр")
    ff, pp = f[sel], p[sel]
    idx = int(np.argmax(pp))
    return {"kind": "arm_resonance", "peak_hz": float(ff[idx]),
            "peak_dbfs_hz": float(dsp.db10(pp[idx])), "freq": ff, "density_dbfs_hz": dsp.db10(pp),
            "note": "пик может включать резонанс тонарм/картридж и низкочастотные помехи проигрывателя/пластины."}


def text_result(r):
    k = r["kind"]
    if k == "frequency_response":
        lines = ["TACET B2 · частотная характеристика; уровни относительно первой ступени 1 кГц",
                 "Частота | Время | L, dB | R, dB | ΔL/R | окон"]
        for q in r["segments"]:
            lines.append(f"{q['frequency_hz']:8.1f} | {q['start_s']:7.2f}–{q['end_s']:7.2f} | "
                         f"{q['L_db']:+7.2f} | {q['R_db']:+7.2f} | {q['delta_lr_db']:+7.2f} | {q['windows']:3d}")
        return "\n".join(lines + [r["note"]])
    if k == "phase":
        return (f"Pink-noise {r['band_low_hz']:.0f}–{r['band_high_hz']:.0f} Гц: "
                f"L={r['left_rms']:.6g} RMS, R={r['right_rms']:.6g} RMS; "
                f"ΔL/R={r['level_delta_db']:+.2f} дБ; корреляция={r['correlation']:+.3f}; "
                f"фаза R−L={r['phase_r_minus_l_deg']:+.1f}°; "
                f"coherence={r['median_coherence']:.2f} ({100*r['coherent_fraction']:.0f}% полосы).\n{r['comment']}")
    if k == "wow_flutter_unweighted":
        return (f"3150 Гц: средняя частота {r['mean_hz']:.3f} Гц; σ={r['std_hz']:.4f} Гц "
                f"({r['std_ppm']:.1f} ppm); peak deviation ±{r['peak_dev_hz']:.3f} Гц.\n{r['note']}")
    if k == "vta_sum":
        return (f"341+659→1000 Гц: 341={r['f1_rms']:.6g} RMS, 659={r['f2_rms']:.6g} RMS, "
                f"1000={r['sum_rms']:.6g} RMS; сумма {r['sum_percent_of_tone_sum']:.5f}% "
                f"({r['sum_dbc_vs_mean_tone']:.1f} dB относительно среднего тона).\n{r['note']}")
    if k == "arm_resonance":
        return f"Резонансный анализ 5–15 Гц: пик {r['peak_hz']:.2f} Гц.\n{r['note']}"
    if k == "vinyl_crosstalk":
        return (f"TACET A4, сигнал только {r['active']}, {r['f_hz']:.0f} Hz: активный канал {r['active_rms']:.6g} RMS, "
                f"утечка {r['leak_rms']:.6g} RMS, crosstalk {r['leak_db']:.1f} dB.\n{r['note']}")
    if k == "skating_300":
        lines = ["300 Гц, растущий уровень; время / L level / L THD / R level / R THD"]
        for q in r["rows"]:
            lines.append(f"{q['time_s']:6.2f}  {q['L_level_dbfs']:8.2f}  {q['L_thd_pct']:9.4f}%  {q['R_level_dbfs']:8.2f}  {q['R_thd_pct']:9.4f}%")
        return "\n".join(lines + [r["note"]])
    return str(r)



B2_FREQUENCIES = np.array([
    20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400,
    500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000,
    6300, 8000, 10000, 12500, 16000, 20000,
], dtype=float)


def analyze_frequency_response(x, fs, window_s=1.0, hop_s=0.25):
    """Автотрекинг синусных ступеней TACET B2 по известному списку частот.

    Оценивает частоту каждого устойчивого окна по FFT и RMS каждого канала
    синусоидальной аппроксимацией. Выход нормируется на первую обнаруженную
    ступень 1 кГц. Длительность ступеней в OCR не указана, поэтому это именно
    автоматическое выделение устойчивых участков, а не точная заводская
    калибровка дорожки.
    """
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = np.column_stack((x, x))
    if x.shape[1] < 2:
        raise ValueError("для TACET B2 нужна стереозапись")
    x = x[:, :2]
    win = int(round(float(window_s) * fs))
    hop = int(round(float(hop_s) * fs))
    if win < fs // 2 or len(x) < win * 2:
        raise ValueError("для B2 нужна запись минимум вдвое длиннее окна анализа")
    fmax = min(20000.0, 0.45 * fs)
    valid_freqs = B2_FREQUENCIES[B2_FREQUENCIES <= fmax]
    if valid_freqs.size < 10:
        raise ValueError("частота дискретизации слишком мала для анализа B2")

    window = np.hanning(win)
    t = np.arange(win, dtype=float) / fs
    mono = np.mean(x, axis=1)
    frames = []
    for start in range(0, len(x) - win + 1, hop):
        block = mono[start:start + win]
        block = block - float(np.mean(block))
        spec = np.abs(np.fft.rfft(block * window))
        freq_axis = np.fft.rfftfreq(win, 1.0 / fs)
        band = (freq_axis >= 15.0) & (freq_axis <= fmax)
        if not np.any(band):
            continue
        inds = np.flatnonzero(band)
        k = int(inds[np.argmax(spec[band])])
        # Параболическая интерполяция вершины спектрального пика в log amplitude.
        delta = 0.0
        if 1 <= k < len(spec) - 1:
            a, b, c = np.log(np.maximum(spec[k - 1:k + 2], 1e-30))
            den = a - 2.0 * b + c
            if abs(den) > 1e-15:
                delta = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
        f_est = float((k + delta) * fs / win)
        idx = int(np.argmin(np.abs(valid_freqs - f_est)))
        f_nom = float(valid_freqs[idx])
        tolerance = max(1.6, f_nom * 0.018)
        if abs(f_est - f_nom) > tolerance:
            frames.append(None)
            continue

        amps = []
        # Модель sinus + cosinus + DC; RMS не зависит от начальной фазы.
        mat = np.column_stack((np.sin(2 * np.pi * f_est * t),
                               np.cos(2 * np.pi * f_est * t), np.ones(win)))
        for c in (0, 1):
            y = x[start:start + win, c]
            coef, *_ = np.linalg.lstsq(mat, y, rcond=None)
            amps.append(float(np.hypot(coef[0], coef[1]) / np.sqrt(2.0)))
        frames.append({"idx": idx, "f_nominal": f_nom, "f_actual": f_est,
                       "time_s": (start + win / 2) / fs,
                       "start_s": start / fs, "end_s": (start + win) / fs,
                       "L_rms": amps[0], "R_rms": amps[1]})

    # Объединяем соседние окна одной частоты в отдельные события/ступени.
    segments = []
    current = []
    current_idx = None
    for frame in frames + [None]:
        if frame is not None and (current_idx is None or frame["idx"] == current_idx):
            if current_idx is None:
                current_idx = frame["idx"]
            current.append(frame)
            continue
        if current:
            segments.append({
                "frequency_hz": float(np.median([q["f_nominal"] for q in current])),
                "actual_hz": float(np.median([q["f_actual"] for q in current])),
                "start_s": float(current[0]["start_s"]),
                "end_s": float(current[-1]["end_s"]),
                "L_rms": float(np.median([q["L_rms"] for q in current])),
                "R_rms": float(np.median([q["R_rms"] for q in current])),
                "windows": len(current),
            })
        current = []
        current_idx = None
        if frame is not None:
            current_idx = frame["idx"]
            current = [frame]

    refs = [q for q in segments if abs(q["frequency_hz"] - 1000.0) < 1.0]
    if not refs:
        raise ValueError("не найдена опорная ступень 1000 Гц; выдели участок с начальным reference 1 кГц")
    ref_l, ref_r = refs[0]["L_rms"], refs[0]["R_rms"]
    for q in segments:
        q["L_db"] = float(dsp.db20(q["L_rms"] / max(ref_l, 1e-20)))
        q["R_db"] = float(dsp.db20(q["R_rms"] / max(ref_r, 1e-20)))
        q["delta_lr_db"] = q["L_db"] - q["R_db"]
    return {"kind": "frequency_response", "segments": segments,
            "reference": {"L_rms": ref_l, "R_rms": ref_r},
            "window_s": win / fs, "hop_s": hop / fs, "band_hi_hz": fmax,
            "note": ("Уровни нормированы на первую найденную ступень 1 кГц. "
                     "Из OCR известны частоты, но не точные записанные амплитуды, времена ступеней и допуски. "
                     "Результат — относительная характеристика всей цепи картридж→корректор→ADC; "
                     "для сравнения RIAA/flat выделяйте соответствующий временной участок.")}

def analyze_crosstalk(x, fs, active="L", f=1000.0):
    """Уровень активного и второго канала на A4 TACET (1 kHz mono, L-only/R-only)."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 1 or x.shape[1] < 2:
        raise ValueError("для crosstalk нужен стерео WAV")
    if len(x) < fs:
        raise ValueError("нужен устойчивый участок не короче 1 секунды")
    active = active.upper()
    if active not in ("L", "R"):
        raise ValueError("active должен быть L или R")
    ca, cb = (0, 1) if active == "L" else (1, 0)
    aa = _tone_rms(x[:, ca], fs, f)
    ab = _tone_rms(x[:, cb], fs, f)
    return {"kind": "vinyl_crosstalk", "active": active, "f_hz": f,
            "active_rms": aa, "leak_rms": ab,
            "leak_db": dsp.db20(ab / max(aa, 1e-20)),
            "note": "включает картридж, тонарм-проводку, кабель, вход фонокорректора и ADC. Повторите на дорожке R-only."}
