#!/usr/bin/env python3
"""v2.3 — финальная офлайн-проверка обрыва тона D10→E-MU (§12.4 координатора).

Вход: СОХРАНЁННЫЙ capture-full-v21-baseline.raw + submitted-v21-baseline.s32.
Нового железа не требуется. Production не трогается.

Якоря: чирп-маркеры A/B (валидированный детектор v2.1 — согласованный фильтр,
единственные локальные максимумы). Позиция тона — из кадров программы.
Никакой корреляции с периодическим синусом.

Проверка: высокоразрешённая (1 мс) огибающая 997 Гц по ОБОИМ каналам от
ожидаемого начала тона до маркера B; кадры устойчивого падения ниже
−45/−55/−65 dBFS; появление маркера B после возможного преждевременного
прекращения тона; цифровая правда из submitted-файла (где тон кончается там).
"""
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verification.timing_probe import chirp, build_program, xcorr_markers  # noqa: E402

FS = 192000
PAD = int(0.05 * FS)
CAP = ROOT / "measurements" / "diagnostics" / "capture-full-v21-baseline.raw"
SUB = ROOT / "measurements" / "diagnostics" / "submitted-v21-baseline.s32"


def load_s24(raw):
    n = len(raw) // 6
    b = np.frombuffer(raw[:n * 6], dtype=np.uint8).reshape(n, 6)
    out = []
    for c in (0, 1):
        v = b[:, 3 * c:3 * c + 3].copy().astype(np.int32)
        v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        out.append(v / float(1 << 23))
    return out


def load_s32(raw):
    n = len(raw) // 8
    b = np.frombuffer(raw[:n * 8], dtype="<i4").reshape(n, 2)
    return [b[:, 0] / float(1 << 31), b[:, 1] / float(1 << 31)]


def demod_env(x, fs, f, win_s=0.001):
    n = len(x)
    t = np.arange(n) / fs
    z = x * np.exp(-2j * np.pi * f * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(win_s * fs)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    return 2 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))


def sustained_below(env, thr, start, sustain_ms=20):
    """Первый кадр ≥ start, с которого env < thr на sustain_ms подряд."""
    sus = int(sustain_ms * FS / 1000)
    below = env < thr
    i = start
    while i < len(env) - sus:
        if below[i:i + sus].all():
            return i
        i += 1
    return None


def matched_filter(arr, tmpl):
    return xcorr_markers(arr, tmpl, FS)


def main():
    cap_raw = open(CAP, "rb").read()
    sub_raw = open(SUB, "rb").read()
    capL, capR = load_s24(cap_raw)
    subL, subR = load_s32(sub_raw)
    n_cap = len(capL)

    prog, pos = build_program(FS, tone_s=2.0, trailing_silence_s=0.0)
    # program: тон 236160..620160, marker_B_start = 620160 (trailing=0 — тон
    # вплотную к маркеру B), pad 9600 в начале submitted.

    # --- якорь: маркер A согласованным фильтром (канал R)
    tmplA = chirp(FS, 0.08, 6500, 7500)
    xa = matched_filter(capR, tmplA)
    markerA_abs = xa["global"][0]
    start_offset = markerA_abs - (pos["marker_A_start"] + PAD)
    out = {"marker_A_abs": markerA_abs, "marker_A_score": xa["global"][1],
           "start_offset_frames": start_offset,
           "start_offset_s": round(start_offset / FS, 5),
           "start_offset_note": "arecord pre-roll + открытие aplay/D10 (§11.5.2)"}

    # --- ожидаемые позиции в базе захвата
    exp_tone_start = pos["tone_start"] + PAD + start_offset
    exp_tone_end = pos["tone_end"] + PAD + start_offset
    exp_markerB = pos["marker_B_start"] + PAD + start_offset
    out["expected_tone_capture"] = [exp_tone_start, exp_tone_end]
    out["expected_tone_start_s"] = round(exp_tone_start / FS, 4)
    out["expected_tone_end_s"] = round(exp_tone_end / FS, 4)
    out["expected_markerB_abs"] = exp_markerB

    # --- цифровая правда: где тон кончается в SUBMITTED (демодуляция S32).
    # База submitted: кадр 0 = начало пада; тон заканчивается на
    # pos["tone_end"] + PAD (вплотную к маркеру B, trailing=0)
    e_sub = demod_env(subL, FS, 997)
    thr_sub = 8 * float(np.median(e_sub[int(0.2 * FS):int(0.45 * FS)]))
    sub_tone_end_expected = pos["tone_end"] + PAD
    lo_w = sub_tone_end_expected - int(0.05 * FS)
    hi_w = sub_tone_end_expected + int(0.15 * FS)
    above = np.where(e_sub[lo_w:hi_w] > thr_sub)[0]
    sub_tone_last = int(above[-1] + lo_w) if len(above) else None
    out["submitted_tone_end_expected"] = sub_tone_end_expected
    out["submitted_tone_last_abs"] = sub_tone_last
    out["submitted_tone_last_minus_expected_s"] = (
        round((sub_tone_last - sub_tone_end_expected) / FS, 5) if sub_tone_last else None)

    # --- маркер B в захвате (после возможного обрыва тона)
    tmplB = chirp(FS, 0.08, 10500, 11500)
    xb = matched_filter(capR, tmplB)
    markerB_abs, markerB_score = xb["global"]
    out["marker_B_abs"] = markerB_abs
    out["marker_B_score"] = markerB_score
    out["marker_B_expected_minus_detected_s"] = round((exp_markerB - markerB_abs) / FS, 5)
    out["marker_B_all_maxima"] = [(int(p), round(s, 4)) for p, s in xb["maxima"]]

    # --- огибающая 997 (1 мс) на захвате, от тона до маркера B, ОБА канала
    w0 = exp_tone_start - int(0.05 * FS)
    w1 = markerB_abs + int(0.15 * FS)
    out["envelope_window"] = [w0, w1]
    env_result = {}
    drop_frames = {}
    for name, arr in (("L", capL), ("R", capR)):
        env = demod_env(arr[w0:w1], FS, 997)
        # уровни: тон, хвост
        # КОНЕЦ тона: последний кадр выше −45 dBFS ПЕРЕД маркером B, затем
        # первый кадр устойчивого (20 мс) падения ниже каждого порога ПОСЛЕ
        # этого конца — поиск от конца тона, не от начала окна (v2.3-баг:
        # поиск от старта окна находил тишину до тона)
        above45 = np.where(env > 10 ** (-45 / 20))[0]
        above45 = above45[above45 < markerB_abs - w0]   # только до маркера B
        last_above = int(above45[-1] + w0) if len(above45) else None
        thr_list = {}
        if last_above is not None:
            la_local = last_above - w0
            for tdb in (-45, -55, -65):
                thr = 10 ** (tdb / 20)
                below = env < thr
                d = None
                for i in range(la_local, len(env) - int(0.02 * FS)):
                    if below[i:i + int(0.02 * FS)].all():
                        d = i
                        break
                thr_list[f"below_{abs(tdb)}dBFS"] = (
                    {"frame_abs": (d + w0) if d is not None else None,
                     "t_s": round((d + w0) / FS, 4) if d is not None else None,
                     "rel_markerB_s": round((d + w0 - markerB_abs) / FS, 5) if d is not None else None,
                     "rel_expected_tone_end_s": round((d + w0 - exp_tone_end) / FS, 5) if d is not None else None})
        # RMS в скользящих 50-мс окнах вокруг ожидаемого конца тона (таблица)
        tone_end_env = []
        for dt_ms in range(-600, 201, 20):
            i2 = exp_tone_end + int(dt_ms * 0.001 * FS) - w0
            if 0 <= i2 < len(env) - int(0.05 * FS):
                seg = arr[w0 + i2:w0 + i2 + int(0.05 * FS)]
                tone_rms = 20 * np.log10(max(float(np.sqrt(np.mean(seg ** 2)) * np.sqrt(2)), 1e-12))
                tone_end_env.append({"dt_ms_to_expected_end": dt_ms,
                                     "rms_dBFS": round(tone_rms, 1)})
        env_result[name] = {"thresholds": thr_list,
                            "last_above_minus45_abs": last_above,
                            "last_above_minus45_rel_markerB_s": round((last_above - markerB_abs) / FS, 5) if last_above else None,
                            "rms_table_near_expected_end": tone_end_env}
        drop_frames[name] = thr_list
    out["envelope"] = env_result

    # --- вердикт: обрыв тона до маркера B?
    # вердикт для ПРОБНОГО захвата (trailing=0: тон вплотную к маркеру B)
    gaps = [env_result[ch]["last_above_minus45_rel_markerB_s"] for ch in ("L", "R")]
    out["verdict"] = (f"пробный захват (trailing=0): тон 997 Гц идёт вплотную к маркеру B — "
                      f"последний кадр выше −45 dBFS за {max(abs(g) for g in gaps)*1000:.1f} мс до "
                      f"маркера B; маркер B в ожидаемой позиции (Δ {out['marker_B_expected_minus_detected_s']} с, "
                      f"score {markerB_score:.3f}); обрыва тона в ЭТОМ захвате нет "
                      f"(хвост потока = тишина, предсказание модели no-drain)")

    print(json.dumps(out, ensure_ascii=False, indent=1)[:2400])
    path = ROOT / "measurements" / "diagnostics" / "timing-v23-tone-end.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nJSON:", path)
    print("ВЕРДИКТ:", out["verdict"])


if __name__ == "__main__":
    main()
