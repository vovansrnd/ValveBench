#!/usr/bin/env python3
"""v2.6 — офлайн-реконструкция lead=0 таймлайна от валидного маркера B
(§14.5 координатора) + joint-LSQ разделение 997 Гц (программный тон) и
1 кГц (остаток preflight).

Без аппаратных прогонов: используются 4 сохранившихся захвата v2.5
(по одному на условие; повторы внутри условия перезаписали друг друга —
ошибка v2.5, признана). Production не изменяется.
"""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verification.timing_probe import chirp  # noqa: E402
from valvebench.backend import SimBackend  # noqa: E402
from valvebench import measure as m, calib  # noqa: E402

FS = 192000
PAD = int(0.05 * FS)
MEASURED_PPM = -23.5          # такт D10 относительно E-MU (preflight v2.1/v2.5)
F_TONE = 997.0 * (1 + MEASURED_PPM * 1e-6)   # 996.9766 Гц — тон в захвате
F_PREFLIGHT = 1000.0 * (1 + MEASURED_PPM * 1e-6)  # 999.9765 Гц — остаток

DIAG = ROOT / "measurements" / "diagnostics"
# (cond-файл, lead_s, trailing_s, run_json) — все четыре захвата являются
# v2.5-программами: [lead][chirp A 0.08][gap 0.15][тон 4.55][trailing]
# [chirp B 0.08][tail 1.0]. SRC-кадры считаются из этой структуры
# (v2.6-исправление: прежняя «probe»-модель 2-с тона была ошибочна —
# замечание координатора §«L0_T0_r1»).
CASES = [
    ("L0_T0_r1", 0.0, 0.0, "v25-run-4-L0_T0_r1.json"),
    ("L1.5_T0_r1", 1.5, 0.0, "v25-run-3-L1.5_T0_r1.json"),
    ("L0_T0.6_r2", 0.0, 0.6, "v25-run-8-L0_T0.6_r2.json"),
    ("L1.5_T0.6_r2", 1.5, 0.6, "v25-run-7-L1.5_T0.6_r2.json"),
]


def src_frames(tone_s, lead_s, trailing_s):
    """Кадры программы v2.5 (как в build_program — детерминированно):
    [lead][chirp A 0.08][gap 0.15][тон 4.55][trailing][chirp B 0.08][tail]"""
    lead_frames = int(round(lead_s * FS))
    mA = lead_frames
    tone_start = mA + int(0.08 * FS) + int(0.15 * FS)
    tone_end = tone_start + int(round(tone_s * FS))
    mB = tone_end + int(round(trailing_s * FS))
    return {"marker_A_start": mA, "tone_start": tone_start,
            "tone_end": tone_end, "marker_B_start": mB}


def norm_xcorr(arr, template):
    n = len(arr); m = len(template)
    L = int(2 ** np.ceil(np.log2(n + m)))
    F = np.fft.rfft(arr, L) * np.conj(np.fft.rfft(template, L))
    cc = np.fft.irfft(F, L)[: n - m + 1]
    energy = np.convolve(arr ** 2, np.ones(m), "valid").clip(1e-30)
    cc = cc[: len(energy)] / (np.linalg.norm(template) * np.sqrt(energy))
    return cc


def marker_detect(arr, tmpl):
    cc = norm_xcorr(arr, tmpl)
    gmax = int(np.argmax(cc)); gscore = float(cc[gmax])
    pk, props = find_peaks(cc, height=0.15, distance=len(tmpl))
    maxima = [(int(p), float(h)) for p, h in zip(pk, props["peak_heights"])]
    if not any(abs(p - gmax) < len(tmpl) // 2 for p, _ in maxima):
        maxima.append((gmax, gscore))
    maxima.sort(key=lambda t: -t[1])
    return {"global": (gmax, gscore), "maxima": maxima}


def local_score(cc, frame, search=500):
    """Локальная нормированная корреляция в ±search кадров вокруг frame."""
    w = cc[max(0, frame - search):frame + search + 1]
    if not len(w):
        return None, None
    i = int(np.argmax(w))
    return float(w[i]), frame - search + i


def joint_lsq_profile(cap, fs, t0, t1, win_s=0.5, hop_s=0.05, f1=F_TONE, f2=F_PREFLIGHT):
    """Совместный МНК-фит двух синусоид (+DC) скользящим окном.
    Возвращает список: {t_center, A_f1_dbfs, A_f2_dbfs, residual_dbfs, n}."""
    out = []
    win = int(round(win_s * fs))
    hop = int(round(hop_s * fs))
    i = t0
    while i + win <= min(t1, len(cap)):
        seg = cap[i:i + win, 0]
        tt = np.arange(win) / fs
        D = np.column_stack([
            np.sin(2 * np.pi * f1 * tt), np.cos(2 * np.pi * f1 * tt),
            np.sin(2 * np.pi * f2 * tt), np.cos(2 * np.pi * f2 * tt),
            np.ones(win)])
        coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
        resid = seg - D @ coef
        a1 = float(np.hypot(coef[0], coef[1]))
        a2 = float(np.hypot(coef[2], coef[3]))
        r_rms = float(np.sqrt(np.mean(resid ** 2)) * np.sqrt(2))
        out.append({
            "t_center_s": round((i + win / 2) / FS, 4),
            "A_997_dbfs": round(20 * np.log10(max(a1, 1e-12)), 1),
            "A_1000_dbfs": round(20 * np.log10(max(a2, 1e-12)), 1),
            "residual_dbfs": round(20 * np.log10(max(r_rms, 1e-12)), 1),
        })
        i += hop
    return out


def main():
    results = []
    offsets_from_B = []
    print("=== Реконструкция таймлайна от валидного маркера B (§14.5.1-2) ===")
    for cond, lead_s, trail_s, run_json in CASES:
        cap_path = DIAG / "v25" / f"v25-capture-{cond}.wav"
        if not cap_path.exists():
            print(f"ПРОПУСК {cond}: capture не сохранился")
            continue
        fsx, cap = wavfile.read(cap_path)
        cap = cap.astype(np.float64)
        run = json.load(open(DIAG / "v25" / run_json))
        src = src_frames(4.55, lead_s, trail_s)

        # маркер B: перепроверка валидированным детектором
        mb = marker_detect(cap[:, 1], chirp(FS, 0.08, 10500, 11500))
        B_abs, B_score = mb["global"]
        # выравнивание: смещение базы захвата относительно программы
        offset_from_B = B_abs - (src["marker_B_start"] + PAD)
        offsets_from_B.append(offset_from_B)

        # ожидаемый кадр маркера A (назад от B)
        src_gap = src["marker_B_start"] - src["marker_A_start"]
        exp_A = B_abs - src_gap
        # локальная корреляция у ожидаемого A (±500 кадров поиск)
        cc = norm_xcorr(cap[:, 1], chirp(FS, 0.08, 6500, 7500))
        local, local_frame = local_score(cc, exp_A)
        # ближайший конкурирующий максимум вне ±500
        pk, props = find_peaks(cc, height=0.15, distance=15360)
        competitors = [(int(p), float(h)) for p, h in zip(pk, props["peak_heights"])
                       if abs(p - exp_A) > 500]
        competitors.sort(key=lambda t: -t[1])

        # ожидаемые кадры тона (назад от Б, по структуре своей программы):
        # маркер Б начинается СРАЗУ после trailing-тишины →
        # tone_end = B_abs − trailing; БЕЗ вычитания длины маркера (0.08 с) —
        # v2.6-ошибка, найденная координатором
        tail_before_B = int(round(trail_s * FS))
        exp_tone_end = B_abs - tail_before_B
        exp_tone_start = exp_tone_end - int(round(4.55 * FS))

        # production onset (unchanged)
        be = SimBackend()
        rig = m.Rig(be, calib.Calibration(), fs=FS, df=1.0, blocks=4, settle=0.5)
        onset = rig._onset(cap, tone_hz=997.0)
        old_w0 = onset + int(0.5 * FS)
        old_w1 = old_w0 + 4 * FS

        # joint-LSQ профиль 997/1000 по всей записи
        prof = joint_lsq_profile(cap, FS, 0, len(cap))
        # классификация зон: 997-доминирование (программный тон)
        tone_chunks = [p for p in prof if p["A_997_dbfs"] > p["A_1000_dbfs"] + 6
                       and p["A_997_dbfs"] > -50]
        first_997 = tone_chunks[0]["t_center_s"] if tone_chunks else None
        last_997 = tone_chunks[-1]["t_center_s"] if tone_chunks else None

        # что пересекает production-окно (по A_997/A_1000 в границах окна)
        win_prof = [p for p in prof if old_w0 <= p["t_center_s"] * FS <= old_w1]
        win_997_max = max((p["A_997_dbfs"] for p in win_prof), default=None)
        win_1000_max = max((p["A_1000_dbfs"] for p in win_prof), default=None)
        win_has_preflight = (win_1000_max is not None and win_997_max is not None
                             and win_1000_max > win_997_max - 6)

        rec = {
            "cond": cond, "lead_s": lead_s, "trailing_s": trail_s,
            "marker_B_abs": B_abs, "marker_B_score": round(B_score, 4),
            "marker_B_maxima_count": len(mb["maxima"]),
            "offset_from_B_frames": offset_from_B,
            "offset_from_B_s": round(offset_from_B / FS, 5),
            "expected_marker_A_abs": exp_A,
            "local_score_at_expected_A": round(local, 4) if local is not None else None,
            "local_max_frame_near_A": local_frame,
            "marker_A_present": bool(local is not None and local > 0.5),
            "nearest_competitor": competitors[0] if competitors else None,
            "expected_tone_abs": [exp_tone_start, exp_tone_end],
            "production_onset_frame": onset,
            "onset_vs_expected_tone_start_s": round((onset - exp_tone_start) / FS, 4),
            "production_window": [old_w0, old_w1],
            "window_intersects": {
                "preflight_1kHz": bool(win_has_preflight),
                "win_997_max_dbfs": win_997_max,
                "win_1000_max_dbfs": win_1000_max,
            },
            "fadeout_start_abs": int(exp_tone_end - int(0.01 * FS)),
            "window_end_to_fadeout_margin_s": round(
                (exp_tone_end - int(0.01 * FS) - old_w1) / FS, 4),
            "tone_997_first_last_chunk_s": [first_997, last_997],
            "joint_lsq_profile": prof,
        }
        results.append(rec)
        print(f"\n=== {cond} (lead={lead_s}, trailing={trail_s})")
        print(f"  маркер Б: {B_abs} score {B_score:.4f}, максимумов: {len(mb['maxima'])}")
        print(f"  offset_from_B: {offset_from_B} кадров ({offset_from_B/FS:.4f} с) "
              f"[arecord pre-roll + pad + открытие — НЕ потеря сэмплов]")
        print(f"  ожидаемый кадр маркера A (назад от Б): {exp_A}")
        print(f"  локальная корреляция у ожидаемого A: {local:.4f} "
              f"(порог наличия 0.5) → маркер A {'ПРИСУТСТВУЕТ' if rec['marker_A_present'] else 'ОТСУТСТВУЕТ'}")
        if competitors:
            print(f"  ближайший конкурирующий максимум: {competitors[0]}")
        print(f"  ожидаемый тон: {exp_tone_start}..{exp_tone_end} "
              f"(t={exp_tone_start/FS:.3f}..{exp_tone_end/FS:.3f} с)")
        print(f"  production onset: {onset} (t={onset/FS:.3f} с), "
              f"vs ожидаемый старт тона: {(onset-exp_tone_start)/FS:+.4f} с")
        print(f"  production-окно пересекает preflight 1 кГц: {win_has_preflight} "
              f"(max 997 = {win_997_max}, max 1000 = {win_1000_max} дБФС в окне)")
        if tone_chunks:
            print(f"  997-доминирующие куска: {first_997}..{last_997} с")

    # кросс-проверка offset между прогонами
    print("\n=== кросс-проверка offset_from_B между прогонами ===")
    for r in results:
        print(f"  {r['cond']}: {r['offset_from_B_frames']} кадров "
              f"({r['offset_from_B_s']} с)")
    vals = [r["offset_from_B_frames"] for r in results]
    print(f"  разброс: {max(vals)-min(vals)} кадров "
          f"({(max(vals)-min(vals))/FS*1000:.1f} мс) — джиттер старта")

    json.dump(results, open(DIAG / "v26-offline-reconstruction.json", "w",
              encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nJSON:", DIAG / "v26-offline-reconstruction.json")


if __name__ == "__main__":
    main()
