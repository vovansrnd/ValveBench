#!/usr/bin/env python3
"""v2.18.1 п.4 — границы тона с РАЗДЕЛЕНИЕМ расписания и измерений.

Исправление ревизии v2.18.1: в v218_tone_bounds.py аномальный onset захвата
№1 (2.25 с) использовался одновременно как ожидаемое начало → поля
head_loss/tail_loss в JSON перепутаны. Теперь:
  * РАСПИСАНИЕ ИСТОЧНИКА (schedule): тон [1.855; 4.305] с — якорь = медиана
    измеренных onset шести нормальных захватов (1.850–1.860) и timeline
    backend (sleep 0.30 + pad 0.05 + lead 1.5 — см. аудит backend.py:574);
  * ФАКТИЧЕСКИЙ onset и конец — измеряются узкополосной демод-огибающей;
  * head_loss = измеренный onset − расписание; tail_loss = расписание-конец
    − измеренный конец;
  * Автоматические assertions: захват №1 — голова 350–420 мс, хвост ≤ 15 мс;
    остальные — голова/хвост ≤ 15 мс. Ошибка воспроизведения = исключение.
  * Production-окно: [onset+0.4; +2.0] — вылет за фактический конец тона
    (для №1 = 350 мс — исправление оценки v2.18 «150 мс»).

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
SUBMITTED_TONE_S = 2.45          # settle 0.4 + 2.0 + запас 0.05 (Rig._seg)


def demod_env_blocks(seg, fs, f_nom=1000.0):
    """Демод-огибающая 5-мс блоков с гашением зеркала 2f (находка v2.18)."""
    n = len(seg)
    t = np.arange(n) / fs
    z = seg * np.exp(-2j * np.pi * f_nom * t)
    per = int(round(fs / (2 * f_nom)))
    if per >= 2:
        z = np.convolve(z, np.ones(per) / per, mode="same")
    blk = int(0.005 * fs)
    m = n // blk
    return 2.0 * np.abs(z[:m * blk].reshape(m, blk).mean(axis=1))


def sustained_bounds(db, thr_dbfs, domain_s, run_ms=50.0):
    """Первый/последний устойчивый (≥run_ms) переход порога в домене."""
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
    results = []
    measured_onsets = {}
    # проход 1: измеренные onset/конец (R-канал, порог −15 дБ от уровня тона)
    for idx in IDX:
        _, cap = wavfile.read(str(Path(args.source_dir) / CAPTURES[idx]))
        cap = cap.astype(np.float64)
        bl = demod_env_blocks(cap[:, 1], FS)
        db = 20 * np.log10(np.clip(bl, 1e-12, None))
        # предварительный старт: грубая зона [1.0, 3.0] с (до уточнения)
        mid = slice(int(1.0 * FS / (0.005 * FS)), int(3.0 * FS / (0.005 * FS)))
        # уровень тона: медиана env в зоне максимального плато (2.0-2.5 с
        # для №1, 1.5-2.5 для остальных — берём максимум медианы скользящих
        # 0.5-с окон, робастно к любому смещению старта)
        win_n = int(0.5 * FS / (0.005 * FS))
        best = -1e30
        for s in range(int(1.5 * FS / (0.005 * FS)),
                       int(4.5 * FS / (0.005 * FS)) - win_n):
            v = float(np.median(db[s:s + win_n]))
            best = max(best, v)
        tone_db = best
        floor_db = float(np.median(db[int(5.6 * FS / (0.005 * FS)):
                                         int(6.8 * FS / (0.005 * FS))]))
        thr = tone_db - 15.0
        s0, e0 = sustained_bounds(db, thr, (0.8, 7.0))
        measured_onsets[idx] = s0
        results.append({"index": idx, "_tmp": {"start": s0, "end": e0,
                                               "tone_db": round(tone_db, 1),
                                               "floor_db": round(floor_db, 1)}})
        print(f"  [{idx:2d}] измеренные: onset {s0:.3f} с, конец {e0:.3f} с | "
              f"тон {tone_db:.1f}, пол {floor_db:.1f} дБФС", flush=True)
    # расписание источника: медиана onset шести нормальных захватов
    normal = [measured_onsets[i] for i in (2, 15, 16, 3, 4, 8)]
    schedule_start = float(np.median(normal))
    schedule_end = schedule_start + SUBMITTED_TONE_S
    print(f"\nрасписание источника: [{schedule_start:.3f}; {schedule_end:.3f}] с "
          f"(медиана onset шести нормальных захватов; timeline backend "
          f"sleep 0.30 + pad 0.05 + lead 1.5 — см. аудит backend.py:574)",
          flush=True)
    # проход 2: потери, окно production, assertions
    final = []
    for r in results:
        idx = r["index"]
        s0 = r["_tmp"]["start"]; e0 = r["_tmp"]["end"]
        head_ms = (s0 - schedule_start) * 1000
        tail_ms = (schedule_end - e0) * 1000
        # production-окно (blocks=2, settle=0.5): [Rig._onset+0.4, +2.0] —
        # onset = DETECTED production onset (v2.16 R-onset: 2.25 у №1),
        # НЕ пороговый crossing демод-огибающей (2.225 — раньше на fade-in)
        prod_onset = {1: 2.25, 2: 1.855, 15: 1.845, 16: 1.855,
                      3: 1.85, 4: 1.855, 8: 1.86}[idx]
        win_start = prod_onset + 0.4
        win_end = win_start + 2.0
        overrun_ms = (win_end - e0) * 1000
        rec = {"index": idx, "level_dbfs": None,
               "schedule_start_s": round(schedule_start, 3),
               "schedule_end_s": round(schedule_end, 3),
               "measured_start_s": round(s0, 3),
               "measured_end_s": round(e0, 3),
               "head_loss_ms": round(head_ms, 1),
               "tail_loss_ms": round(tail_ms, 1),
               "production_window_s": [round(win_start, 3), round(win_end, 3)],
               "window_overrun_past_tone_end_ms": round(overrun_ms, 1)}
        # assertions (ревизия v2.18.1: ошибка не должна повториться)
        if idx == 1:
            assert 350.0 <= head_ms <= 420.0, \
                f"idx1: голова {head_ms:.0f} мс вне 350–420 (аудит/ревизия)"
            assert tail_ms <= 15.0, f"idx1: хвост {tail_ms:.0f} мс > 15"
            assert 350.0 <= overrun_ms <= 370.0, \
                f"idx1: вылет окна {overrun_ms:.0f} мс вне 350–370"
        else:
            assert abs(head_ms) <= 15.0, \
                f"idx{idx}: голова {head_ms:.0f} мс > 15 у нормального захвата"
            assert abs(tail_ms) <= 15.0, f"idx{idx}: хвост {tail_ms:.0f} мс > 15"
        rec["assertions"] = "PASS"
        final.append(rec)
        print(f"  [{idx:2d}] голова {head_ms:+.0f} мс, хвост {tail_ms:+.0f} мс | "
              f"окно [{win_start:.3f},{win_end:.3f}] вылет за тон {overrun_ms:+.0f} мс | "
              f"assertions PASS", flush=True)
    # r._tmp уровни в финал
    for rec, r in zip(final, results):
        rec["tone_level_dbfs"] = r["_tmp"]["tone_db"]
        rec["floor_dbfs"] = r["_tmp"]["floor_db"]
        del rec["level_dbfs"]
    json.dump({"schedule": {"start_s": round(schedule_start, 3),
                            "end_s": round(schedule_end, 3),
                            "anchor": "медиана onset шести нормальных захватов; "
                                      "timeline backend.py:574 (sleep 0.30) + "
                                      "pad 0.05 + lead 1.5"},
               "results": final,
               "assertions": "все PASS (голова №1 350–420 мс, хвост ≤15 мс; "
                             "остальные ≤15 мс; вылет окна №1 350–370 мс)"},
              open(OUT / "v2181-tone-bounds.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v2181-tone-bounds.json")


if __name__ == "__main__":
    main()
