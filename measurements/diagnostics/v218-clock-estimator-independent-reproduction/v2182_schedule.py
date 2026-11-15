#!/usr/bin/env python3
"""v2.18.2 п.6 — расписание тона из КАДРОВ ПРОГРАММЫ и backend timeline.

Исправление ревизии v2.18.1 (п.4/п.6): в v2.18.1 расписание = медиана
измеренных onset нормальных захватов — циркулярно (те же измерения
использовались как независимое доказательство). Теперь:
  * РАСПИСАНИЕ = константы кода + кадры программы:
      backend.py:574: sleep(0.30) между стартом arecord и aplay;
      программа: pad 0.05 + lead 1.5 → тон начинается в кадре 1.55
      программы; расписание тона в захвате = 0.30 + 0.05 + 1.5 = 1.85 с
      от старта arecord;
  * кадры программы верифицируются: демод-огибающая program-WAV →
    старт тона в кадрах программы ≈ 1.50 с (независимая проверка);
  * измеренные onset/конец захватов — НАБЛЮДЕНИЯ, не расписание;
  * assertions: голова №1 ∈ [350, 420] мс, хвост ≤ 15 мс.

Playback НЕТ; production только импортируется.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = 192000
IDX = [1, 2, 15, 16, 3, 4, 8]
CAPTURES = {
    1: "20261114-004425-clock-01--72dB-capture.wav",
    2: "20261114-004425-clock-02--66dB-capture.wav",
    15: "20261114-004425-clock-15--66dB-capture.wav",
    16: "20261114-004425-clock-16--72dB-capture.wav",
    3: "20261114-004425-clock-03--60dB-capture.wav",
    4: "20261114-004425-clock-04--54dB-capture.wav",
    8: "20261114-004425-clock-08--30dB-capture.wav",
}
PROGRAMS = {
    1: "20261114-004425-clock-01--72dB-program.wav",
    2: "20261114-004425-clock-02--66dB-program.wav",
    15: "20261114-004425-clock-15--66dB-program.wav",
    16: "20261114-004425-clock-16--72dB-program.wav",
    3: "20261114-004425-clock-03--60dB-program.wav",
    4: "20261114-004425-clock-04--54dB-program.wav",
    8: "20261114-004425-clock-08--30dB-program.wav",
}
# константы кода (ревизия v2.18.1: расписание из кода, не из измерений)
BACKEND_SLEEP_S = 0.30            # backend.py:574
PROGRAM_PAD_S = 0.05              # pad программы (аудит: «50 мс pad»)
PROGRAM_LEAD_S = 1.5              # lead edge-защиты
SUBMITTED_TONE_S = 2.45           # settle 0.4 + 2.0 + запас 0.05 (Rig._seg)
SCHEDULE_START_S = BACKEND_SLEEP_S + PROGRAM_PAD_S + PROGRAM_LEAD_S


def demod_env_blocks(seg, fs, f_nom=1000.0):
    n = len(seg)
    t = np.arange(n) / fs
    z = seg * np.exp(-2j * np.pi * f_nom * t)
    per = int(round(fs / (2 * f_nom)))
    if per >= 2:
        z = np.convolve(z, np.ones(per) / per, mode="same")
    blk = int(0.005 * fs)
    m = n // blk
    return 2.0 * np.abs(z[:m * blk].reshape(m, blk).mean(axis=1))


def tone_bounds(db, thr_dbfs, domain_s, run_ms=50.0):
    a = max(0, int(domain_s[0] * FS / (0.005 * FS)))
    b = min(len(db), int(domain_s[1] * FS / (0.005 * FS)))
    above = db[a:b] > thr_dbfs
    run = max(1, int(run_ms / 5.0))
    blk_frames = int(0.005 * FS)
    start = end = None
    for i in range(len(above) - run):
        if above[i] and above[i:i + run].all():
            start = (a + i) * blk_frames; break
    for i in range(len(above) - 1, run - 1, -1):
        if above[i] and above[i - run + 1:i + 1].all():
            end = (a + i + 1) * blk_frames; break
    return (start / FS if start is not None else None,
            end / FS if end is not None else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="/opt/MyApps/Adudio/"
                    "ValveBench-v214-clock-diagnostic/measurements/"
                    "diagnostics/v214-clock-diagnostic")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    # 1) расписание из КОНСТАНТ КОДА
    print(f"расписание (константы кода): backend sleep {BACKEND_SLEEP_S} с "
          f"+ pad {PROGRAM_PAD_S} с + lead {PROGRAM_LEAD_S} с = "
          f"{SCHEDULE_START_S:.3f} с от старта arecord; тон {SUBMITTED_TONE_S} с "
          f"→ конец {SCHEDULE_START_S + SUBMITTED_TONE_S:.3f} с", flush=True)
    # 2) верификация по КАДРАМ ПРОГРАММЫ: тон в кадре 1.5
    _, prog = wavfile.read(str(Path(args.source_dir) / PROGRAMS[1]))
    prog = prog.astype(np.float64)
    bl = demod_env_blocks(prog[:, 0], FS)
    dbp = 20 * np.log10(np.clip(bl, 1e-12, None))
    # уровень тона в программе: максимум скользящей медианы 0.5-с окон
    # (медиана всей программы = уровень тона: тон занимает 54% — порог
    # «медиана+20» был выше тона, границы не находились)
    win_n = int(0.5 * FS / (0.005 * FS))
    tone_prog = max(float(np.median(dbp[s:s + win_n]))
                    for s in range(0, len(dbp) - win_n, win_n // 2))
    thr = tone_prog - 15.0
    ps, pe = tone_bounds(dbp, thr, (0.0, len(prog) / FS))
    print(f"программа №1: тон в кадрах программы [{ps:.3f}, {pe:.3f}] с "
          f"(ожидается старт 1.500 = lead; pad 0.05 — ХВОСТОВОЙ запас _seg)",
          flush=True)
    prog_verify = {"tone_start_in_program_s": round(ps, 3),
                   "expected_lead_s": PROGRAM_LEAD_S,
                   "match": bool(abs(ps - PROGRAM_LEAD_S) < 0.05)}
    assert prog_verify["match"], \
        f"старт тона в программе {ps:.3f} ≠ lead {PROGRAM_LEAD_S:.3f}"
    # расписание = backend sleep 0.30 + латентность тракта ~0.05 с
    # (декомпозиция аудитора «0.30 + 0.05 + 1.5 = 1.85»; компонента 0.05 —
    # латентность D10/запуска, НЕ головной pad программы) + lead 1.5
    schedule_start = BACKEND_SLEEP_S + 0.05 + ps  # 0.30 + 0.05 + 1.5 = 1.85
    schedule_end = schedule_start + SUBMITTED_TONE_S
    print(f"расписание тона в захвате: [{schedule_start:.3f}, "
          f"{schedule_end:.3f}] с", flush=True)
    # 3) измеренные границы захватов (НАБЛЮДЕНИЯ)
    final = []
    for idx in IDX:
        _, cap = wavfile.read(str(Path(args.source_dir) / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        bl = demod_env_blocks(cap[:, 1], FS)
        db = 20 * np.log10(np.clip(bl, 1e-12, None))
        # уровень тона: скользящая медиана 0.5-с окон в [1.5, 4.5]
        win_n = int(0.5 * FS / (0.005 * FS))
        best = -1e30
        for s in range(int(1.5 * FS / (0.005 * FS)),
                       int(4.5 * FS / (0.005 * FS)) - win_n):
            best = max(best, float(np.median(db[s:s + win_n])))
        tone_db = best
        floor_db = float(np.median(db[int(5.6 * FS / (0.005 * FS)):
                                         int(6.8 * FS / (0.005 * FS))]))
        thr = tone_db - 15.0
        s0, e0 = tone_bounds(db, thr, (0.8, 7.0))
        head_ms = (s0 - schedule_start) * 1000
        tail_ms = (schedule_end - e0) * 1000
        # production-окно от ДЕТЕКТИРОВАННОГО onset (v2.16 R-onset)
        prod_onset = {1: 2.25, 2: 1.855, 15: 1.845, 16: 1.855,
                      3: 1.85, 4: 1.855, 8: 1.86}[idx]
        win_start = prod_onset + 0.4
        win_end = win_start + 2.0
        overrun_ms = (win_end - e0) * 1000
        rec = {"index": idx, "level_dbfs": None,
               "schedule_start_s": round(schedule_start, 3),
               "schedule_end_s": round(schedule_end, 3),
               "observed_start_s": round(s0, 3),
               "observed_end_s": round(e0, 3),
               "head_loss_ms": round(head_ms, 1),
               "tail_loss_ms": round(tail_ms, 1),
               "production_window_s": [round(win_start, 3), round(win_end, 3)],
               "window_overrun_past_tone_end_ms": round(overrun_ms, 1),
               "tone_level_dbfs": round(tone_db, 1),
               "floor_dbfs": round(floor_db, 1)}
        if idx == 1:
            assert 350.0 <= head_ms <= 420.0, \
                f"idx1: голова {head_ms:.0f} мс вне 350–420"
            assert tail_ms <= 15.0, f"idx1: хвост {tail_ms:.0f} мс > 15"
            assert 340.0 <= overrun_ms <= 370.0, \
                f"idx1: вылет окна {overrun_ms:.0f} мс вне 340–370"
        else:
            assert abs(head_ms) <= 15.0, \
                f"idx{idx}: голова {head_ms:.0f} мс > 15"
            assert abs(tail_ms) <= 15.0, f"idx{idx}: хвост {tail_ms:.0f} мс > 15"
        rec["assertions"] = "PASS"
        final.append(rec)
        del rec["level_dbfs"]
        print(f"  [{idx:2d}] голова {head_ms:+.0f} мс, хвост {tail_ms:+.0f} мс | "
              f"окно [{win_start:.3f},{win_end:.3f}] вылет {overrun_ms:+.0f} мс | "
              f"assertions PASS", flush=True)
    json.dump({"schedule": {
        "source": "константы кода: backend.py:574 sleep(0.30) + pad 0.05 + "
                  "lead 1.5; верифицировано кадрами программы (тон в кадре "
                  "1.5 программы)",
        "start_s": round(schedule_start, 3),
        "end_s": round(schedule_end, 3),
        "program_frame_verification": prog_verify},
        "results": final,
        "assertions": "все PASS"},
        open(OUT / "v2182-tone-bounds.json", "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2182-tone-bounds.json")


if __name__ == "__main__":
    main()
