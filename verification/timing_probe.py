#!/usr/bin/env python3
"""timing_probe v2 — посемпловая характеристика D10→E-MU (корректирующее
задание координатора §11.5 после ревизии v1).

v1 ошибки, исправленные здесь (см. §11 handoff-файла):
  * смещения срезов прибавлялись неверно → все «события» были одним кадром;
    теперь: детекция в АБСОЛЮТНЫХ координатах захвата, в JSON сохраняются и
    локальный индекс, и начало среза, и абсолютный кадр каждого события;
  * конечный маркер 8 кГц коллизировал с H8 тона (7976 Гц) → заменён на
    ЛИНЕЙНЫЕ ЧИРП-маркеры (наклонные свипы) и детектируются согласованным
    фильтром по всей записи: в JSON пишутся ВСЕ пики выше порога — ложные
    срабатывания видны явно;
  * «latency 0.75 с» смешивала время; теперь модель временной базы явная:
    capture t=0 = старт arecord; aplay стартует позже (замер по часам);
    в поток добавлен пад 0.05 с; ожидаемые позиции событий пересчитаны в
    базу захвата через стартовое смещение (определяемое по СТАРТ-маркеру);
    величина называется start_offset_s — это НЕ «латентность D10»;
  * результаты каждого прогона пишутся в ОТДЕЛЬНЫЙ файл (v1 перезаписывала);
    полный сырой захват сохраняется для базового прогона.

Программа: пад 0.05 | тишина 1.0 | ЧИРП-А 6.5→7.5 кГц 80 мс | тишина 0.15 |
тон 997 (tone_s) | [тишина trailing_silence_s] | ЧИРП-Б 10.5→11.5 кГц 80 мс |
тишина 1.0.
"""
import argparse
import json
import os
import subprocess
import time
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAC_USBID = "152a:85dd"   # SMSL D10
ADC_USBID = "041e:3f04"   # E-MU 0404


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


def chirp(fs, dur, f0, f1, level_dbfs=-30.0):
    n = int(round(dur * fs))
    t = np.arange(n) / fs
    k = (f1 - f0) / dur
    ph = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    s = 10 ** (level_dbfs / 20) * np.sin(ph)
    fade = int(0.01 * fs)
    s[:fade] *= np.linspace(0, 1, fade)
    s[-fade:] *= np.linspace(1, 0, fade)
    return s


def build_program(fs, tone_s, trailing_silence_s=0.0):
    """Позиции сегментов: pos[name+"_start"/"_end"] в кадрах программы.
    Явные start/end вместо v2-ошибки, где fr[name] был стартом сегмента."""
    P = []
    pos = {}
    cur = [0]
    def seg(dur, sig=None, name=None):
        n = int(round(dur * fs))
        if name:
            pos[name + "_start"] = cur[0]
        P.append(np.zeros((n, 2)) if sig is None else sig)
        cur[0] += len(P[-1])
        if name:
            pos[name + "_end"] = cur[0]
    seg(1.0, name="lead")
    seg(0.08, np.column_stack([chirp(fs, 0.08, 6500, 7500)] * 2), name="marker_A")
    seg(0.15, name="gap1")
    nt = int(round(tone_s * fs))
    tone = 10 ** (-30 / 20) * np.sin(2 * np.pi * 997 * np.arange(nt) / fs)
    fade = int(0.01 * fs)
    tone[:fade] *= np.linspace(0, 1, fade)
    tone[-fade:] *= np.linspace(1, 0, fade)
    seg(tone_s, np.column_stack([tone, tone]), name="tone")
    if trailing_silence_s:
        seg(trailing_silence_s, name="trailing")
    seg(0.08, np.column_stack([chirp(fs, 0.08, 10500, 11500)] * 2), name="marker_B")
    seg(1.0, name="tail")
    pos["program_len"] = cur[0]
    return np.concatenate(P), pos


def xcorr_markers(capture, template, fs):
    """v2.1 (§12.1): согласованный фильтр с ПОЛНЫМ перебором локальных максимумов.

    v2-баг: группировка переходов порога + пропуск 0.1*fs после каждой группы
    (чирп 80 мс!) — пропускала главный пик и выбирала точку на склоне лепестка
    (298015/0.491 вместо 298152/0.9951 на сохранённом захвате).

    v2.1: scipy.find_peaks с минимальным разносом = длина шаблона (обоснование:
    главный лепесток автокорреляции линейного чирпа ≈ 1/полоса = 1 мс, длина
    шаблона 80 мс — консервативный разнос, исключающий собственные лепестки).
    Возвращает: global (кадр, score), список ВСЕХ локальных максимумов
    (кадр, score), и нигде не пропускает интервалы."""
    from scipy.signal import find_peaks
    n = len(capture); m = len(template)
    L = int(2 ** np.ceil(np.log2(n + m)))
    F = np.fft.rfft(capture, L) * np.conj(np.fft.rfft(template, L))
    cc = np.fft.irfft(F, L)[: n - m + 1]
    energy = np.convolve(capture ** 2, np.ones(m), "valid").clip(1e-30)
    cc = cc[: len(energy)] / (np.linalg.norm(template) * np.sqrt(energy))
    gmax = int(np.argmax(cc)); gscore = float(cc[gmax])
    pk, props = find_peaks(cc, height=0.15, distance=m)
    # глобальный максимум мог быть на краю (не локальный пик find_peaks) — добавить
    allmax = [(int(p), float(h)) for p, h in zip(pk, props["peak_heights"])]
    if not any(abs(p - gmax) < m // 2 for p, _ in allmax):
        allmax.append((gmax, gscore))
    allmax.sort(key=lambda t: -t[1])
    return {"global": (gmax, gscore), "maxima": allmax}


def demod_env(x, fs, f, win_s=0.005):
    n = len(x)
    t = np.arange(n) / fs
    z = x * np.exp(-2j * np.pi * f * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(win_s * fs)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    return 2 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))


def run_case(fs, tone_s, buffer_ms, trailing_silence_s, hw_dac, hw_adc,
             save_raw=False, outdir=".", tag="run"):
    prog, fr = build_program(fs, tone_s, trailing_silence_s)
    pad_n = int(0.05 * fs)
    submitted = np.concatenate([np.zeros((pad_n, 2)), prog, np.zeros((pad_n, 2))])
    iv = np.round(np.clip(submitted, -1, 1) * (1 << 31)).astype("<i4")  # DX1-класс требует S32; D10 принимает S32_LE тоже
    sub_path = f"/tmp/tp_sub_{tag}.s32"
    iv.tofile(sub_path)

    rec_s = len(submitted) / fs + 3.0
    cap_path = f"/tmp/tp_cap_{tag}.raw"
    t_cap0 = time.monotonic()
    cap = subprocess.Popen(
        ["arecord", "-D", f"hw:{hw_adc},0", "-f", "S24_3LE", "-r", str(fs),
         "-c", "2", "-d", str(int(rec_s) + 1), "-t", "raw", cap_path],
        stderr=subprocess.PIPE)
    time.sleep(0.5)
    t_play0 = time.monotonic()
    play = subprocess.run(
        ["aplay", "-D", f"hw:{hw_dac},0", "-f", "S32_LE", "-r", str(fs),
         "-c", "2", "-q", "--buffer-time", str(buffer_ms * 1000), sub_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    t_play1 = time.monotonic()
    cap.wait()
    cap_err = cap.stderr.read().decode("utf-8", "replace")

    raw = open(cap_path, "rb").read()
    n = len(raw) // 6
    b = np.frombuffer(raw[:n * 6], dtype=np.uint8).reshape(n, 6)
    chans = []
    for c in (0, 1):
        v = b[:, 3 * c:3 * c + 3].copy().astype(np.int32)
        v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        chans.append(v / float(1 << 23))
    capL, capR = chans

    res = {
        "tone_s": tone_s, "buffer_ms": buffer_ms,
        "trailing_silence_s": trailing_silence_s,
        "arecord_prestart_s": round(t_play0 - t_cap0, 4),
        "aplay_elapsed_s": round(t_play1 - t_play0, 4),
        "aplay_rc": play.returncode,
        "aplay_err": play.stderr.decode("utf-8", "replace").strip()[:120],
        "arecord_err": cap_err.strip()[:120],
        "submitted_frames": len(submitted),
        "captured_frames": n,
        "pad_frames": pad_n,
    }
    events = {}
    SEL_MIN_SCORE = 0.5   # критерий выбора: score глобального максимума ≥ 0.5
    for c, name, arr in ((0, "L", capL), (1, "R", capR)):
        ev = {}
        tmplA = chirp(fs, 0.08, 6500, 7500)
        xa = xcorr_markers(arr, tmplA, fs)
        ev["marker_A_global_abs"], ev["marker_A_global_score"] = xa["global"]
        ev["marker_A_all_maxima"] = [(int(p), round(s, 4)) for p, s in xa["maxima"]]
        ev["marker_A_selected_score_ok"] = bool(ev["marker_A_global_score"] >= SEL_MIN_SCORE)
        tmplB = chirp(fs, 0.08, 10500, 11500)
        xb = xcorr_markers(arr, tmplB, fs)
        ev["marker_B_global_abs"], ev["marker_B_global_score"] = xb["global"]
        ev["marker_B_all_maxima"] = [(int(p), round(s, 4)) for p, s in xb["maxima"]]
        ev["marker_B_selected_score_ok"] = bool(ev["marker_B_global_score"] >= SEL_MIN_SCORE)
        # тон: порог от самой тихой 0.15-с области ДО маркера А
        e997 = demod_env(arr, fs, 997)
        mA = ev["marker_A_global_abs"]
        quiet = []
        if mA:
            for i in range(int(0.2 * fs), max(int(0.25 * fs), mA - int(0.2 * fs)), int(0.15 * fs)):
                quiet.append(float(np.median(e997[i:i + int(0.15 * fs)])))
        thr = max(8 * (min(quiet) if quiet else 1e-7), 1e-6)
        ev["tone_thr"] = thr
        ev["tone_search_slice_start_abs"] = None
        ev["tone_first_local"] = None
        if mA:
            # поиск тона ПОСЛЕ конца маркера А: пик согласованного фильтра —
            # левый край совпадения, сам маркер длится 0.08 с и его
            # огибающая на 997 Гц триггерит детектор (ошибка v1/v2.1)
            w0 = mA + int(0.10 * fs)
            w1 = min(mA + int(0.35 * fs) + int(tone_s * fs) + int(2.5 * fs), n)
            ev["tone_search_slice_start_abs"] = w0
            seg = e997[w0:w1]
            above = np.where(seg > thr)[0]
            if len(above):
                ev["tone_first_local"] = int(above[0])
                ev["tone_first_abs"] = int(above[0] + w0)
                ev["tone_last_abs"] = int(above[-1] + w0)
        events[name] = ev
    res["events"] = events

    mA = events["L"]["marker_A_global_abs"]
    if mA is not None:
        start_offset = mA - (fr["marker_A_start"] + pad_n)
        res["start_offset_s"] = round(start_offset / fs, 5)
        res["start_offset_note"] = ("смещение старта программы в базе захвата: arecord pre-roll + "
                                    "открытие aplay/D10; НЕ является латентностью D10 (§11.5.2)")
        res["expected_abs"] = {
            "marker_A_start": fr["marker_A_start"] + pad_n + start_offset,
            "tone_start": fr["tone_start"] + pad_n + start_offset,
            "tone_end": fr["tone_end"] + pad_n + start_offset,
            "marker_B_start": fr["marker_B_start"] + pad_n + start_offset,
        }
        for name in ("L", "R"):
            for key in ("tone_last_abs", "tone_contiguous_last_abs"):
                tl = events[name].get(key)
                if tl is not None:
                    res[f"{key.replace('_abs','')}_tail_s_{name}"] = round(
                        (tl - res["expected_abs"]["tone_end"]) / fs, 4)
        # прямой RMS-замер послетоновой зоны: [tone_end .. tone_end+0.4 с]
        # против зоны тона — если D10 продлевает тон, там будет тон, а не шум
        te = res["expected_abs"]["tone_end"]
        ts = res["expected_abs"]["tone_start"]
        post = int(0.4 * fs)
        for name, arr in (("L", capL), ("R", capR)):
            if te + post < n and ts < te:
                r_tone = 20 * np.log10(max(float(np.sqrt(np.mean(arr[ts + int(0.3*fs):te - int(0.1*fs)]**2)) * 2**0.5), 1e-12))
                r_post = 20 * np.log10(max(float(np.sqrt(np.mean(arr[te:te + post]**2)) * 2**0.5), 1e-12))
                res[f"rms_tone_dBFS_{name}"] = round(r_tone, 1)
                res[f"rms_post_tone_dBFS_{name}"] = round(r_post, 1)
        mB = events["R"]["marker_B_global_abs"]
        res["marker_B_expected_minus_detected_s"] = round(
            (res["expected_abs"]["marker_B_start"] - mB) / fs, 5)
        res["marker_B_score"] = events["R"]["marker_B_global_score"]
        res["marker_A_score"] = events["L"]["marker_A_global_score"]

    if save_raw:
        os.makedirs(outdir, exist_ok=True)
        import shutil
        shutil.copy(cap_path, os.path.join(outdir, f"capture-full-{tag}.raw"))
        shutil.copy(sub_path, os.path.join(outdir, f"submitted-{tag}.s32"))
        res["saved_capture"] = f"capture-full-{tag}.raw (S24_3LE stereo, {fs} Hz)"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tone", type=float, default=2.0)
    ap.add_argument("--trailing", type=float, default=0.0)
    ap.add_argument("--buffer", type=int, default=100)
    ap.add_argument("--save-raw", action="store_true")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--rate", type=int, default=192000)
    a = ap.parse_args()
    dac, adc = find_card(DAC_USBID), find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    outdir = os.path.join(ROOT, "measurements", "diagnostics")
    os.makedirs(outdir, exist_ok=True)
    res = run_case(a.rate, a.tone, a.buffer, a.trailing, dac, adc,
                   save_raw=a.save_raw, outdir=outdir, tag=a.tag)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    path = os.path.join(outdir, f"timing-v2-{a.tag}.json")
    json.dump(res, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("JSON:", path, flush=True)


if __name__ == "__main__":
    main()
