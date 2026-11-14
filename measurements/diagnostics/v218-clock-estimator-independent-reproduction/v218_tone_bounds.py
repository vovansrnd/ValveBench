#!/usr/bin/env python3
"""v2.18 п.4 — границы тона во всех семи захватах v2.18/2.14 с явной
чувствительностью к порогу. Проверка спора: потеряна ГОЛОВА ~400 мс
(версия аудитора) или ХВОСТ ~185 мс (утверждение v2.17).

Исправление методики v2.17: в v2.17 скан края начинался с блока 900
(4.500 с) и «край тона 4.500 с» был артефактом точки старта скана. Здесь
скан ведётся по ВСЕЙ записи, граница = последний/первый УСТОЙЧИВЫЙ (≥50 мс)
переход порога демод-огибающей 1 кГц; порог = уровень тона − δ дБ,
δ ∈ {6, 10, 15, 20} — зависимость результата от порога показана явно.
Дополнительно: поиск шума-всплеска в [4.2, 5.0] захвата №1 (гипотеза
аудитора о «шумовом пике»).

Playback НЕТ; production только импортируется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SRC = Path("/opt/MyApps/Adudio/ValveBench-v214-clock-diagnostic/"
           "measurements/diagnostics/v214-clock-diagnostic")
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
R_ONSET = {1: 2.25, 2: 1.855, 15: 1.845, 16: 1.855,
           3: 1.85, 4: 1.855, 8: 1.86}
SUBMITTED_TONE_S = 2.45          # settle 0.4 + 2.0 + запас 0.05


def demod_env_blocks(seg, fs, f_nom=1000.0):
    """Демод-огибающая, 5-мс блоки, с ГАШЕНИЕМ ЗЕРКАЛА 2f (скользящее
    среднее ровно в период зеркала fs/(2·f_nom)) — см. находку v2.18."""
    n = len(seg)
    t = np.arange(n) / fs
    z = seg * np.exp(-2j * np.pi * f_nom * t)
    per = int(round(fs / (2 * f_nom)))
    if per >= 2:
        z = np.convolve(z, np.ones(per) / per, mode="same")
    blk = int(0.005 * fs)
    m = n // blk
    # когерентная амплитуда блока: 2·|mean(z)| — шум усредняется, уровень
    # тона не завышается (max|z| в блоке давал +3 дБ на шумном L)
    return 2.0 * np.abs(z[:m * blk].reshape(m, blk).mean(axis=1))


def sustained_bounds(db, thr_dbfs, domain_s, run_ms=50.0):
    """Первый и последний УСТОЙЧИВЫЙ (≥ run_ms) переход порога в домене
    domain_s = (t_start, t_end) — скан ИСКЛЮЧАЕТ pre-roll мусор (жумп/остаток
    предыдущего потока в первых ~0.6 с захвата). Ошибка v2.17: скан края
    начинался с блока 900 (4.500 с) — граница была артефактом старта скана."""
    a = max(0, int(domain_s[0] * FS / (0.005 * FS)))
    b = min(len(db), int(domain_s[1] * FS / (0.005 * FS)))
    above = db[a:b] > thr_dbfs
    run = max(1, int(run_ms / 5.0))
    blk_frames = int(0.005 * FS)          # 960 кадров на блок 5 мс
    start = end = None
    for i in range(len(above) - run):
        if above[i] and above[i:i + run].all():
            start = (a + i) * blk_frames; break
    for i in range(len(above) - 1, run - 1, -1):
        if above[i] and above[i - run + 1:i + 1].all():
            end = (a + i + 1) * blk_frames; break
    return (start / FS if start is not None else None,
            end / FS if end is not None else None)


def analyze_capture(idx):
    _, cap = wavfile.read(str(SRC / CAPTURES[idx]))
    cap = cap.astype(np.float64)
    out = {"index": idx, "windows_s": round(len(cap) / FS, 3), "channels": {}}
    onset = R_ONSET[idx]
    submitted = (onset, onset + SUBMITTED_TONE_S)
    out["submitted_tone"] = [round(onset, 3), round(onset + SUBMITTED_TONE_S, 3)]
    for c, ch in ((0, "L"), (1, "R")):
        bl = demod_env_blocks(cap[:, c], FS)
        db = 20 * np.log10(np.clip(bl, 1e-12, None))
        # уровень тона: медиана демод-env в середине ожидаемого тона
        mid = (int((onset + 0.6) * FS / (0.005 * FS)),
               int((onset + 1.6) * FS / (0.005 * FS)))
        tone_db = float(np.median(db[mid[0]:mid[1]]))
        # пол: зона [5.6, 6.8] с — после программы, заведомо тихая
        # (зона [0, 0.8] содержит pre-roll мусор — завышала пол на 8+ дБ)
        floor_lo = int(5.6 * FS / (0.005 * FS))
        floor_hi = int(6.8 * FS / (0.005 * FS))
        floor_db = float(np.median(db[floor_lo:floor_hi]))
        ch_out = {"tone_level_dbfs": round(tone_db, 1),
                  "floor_dbfs": round(floor_db, 1),
                  "floor_region_s": [5.6, 6.8],
                  "bounds_by_threshold": {}}
        for delta in (6.0, 10.0, 15.0, 20.0):
            thr = tone_db - delta
            s, e = sustained_bounds(db, thr, (max(0.0, onset - 0.5), 7.0))
            head = None if s is None else round((s - submitted[0]) * 1000, 1)
            tail = None if e is None else round((submitted[1] - e) * 1000, 1)
            ch_out["bounds_by_threshold"][f"tone-{delta:.0f}dB"] = {
                "threshold_dbfs": round(thr, 1),
                "start_s": round(s, 3) if s is not None else None,
                "end_s": round(e, 3) if e is not None else None,
                "head_loss_ms": head, "tail_loss_ms": tail,
                "tone_span_ms": (round((e - s) * 1000, 1)
                                 if s is not None and e is not None else None)}
        out["channels"][ch] = ch_out
        parts = []
        for dd, v in ((6.0, ch_out["bounds_by_threshold"]["tone-6dB"]),
                      (10.0, ch_out["bounds_by_threshold"]["tone-10dB"]),
                      (15.0, ch_out["bounds_by_threshold"]["tone-15dB"]),
                      (20.0, ch_out["bounds_by_threshold"]["tone-20dB"])):
            parts.append(f"−{dd:.0f}дБ: [{v['start_s']},{v['end_s']}] "
                         f"голова {v['head_loss_ms']} хвост {v['tail_loss_ms']}")
        print(f"  [{idx:2d}] {ch}: тон {tone_db:.1f} дБФС, пол {floor_db:.1f} | "
              + " | ".join(parts), flush=True)
    # захват №1: плотный профиль демод-env R в [4.0, 5.0] — всплеск?
    if idx == 1:
        bl = demod_env_blocks(cap[:, 1], FS)
        db = 20 * np.log10(np.clip(bl, 1e-12, None))
        zone = db[int(4.0 * FS / (0.005 * FS)):int(5.0 * FS / (0.005 * FS))]
        prof = {round(4.0 + i * 0.005, 3): round(float(v), 1)
                for i, v in enumerate(zone) if i % 8 == 0}
        out["idx1_R_env_4_5s"] = prof
        burst = [t for t, v in prof.items() if v > -90]
        out["idx1_burst_above_-90dBFS_after_4.0s"] = burst
        print(f"  [ 1] R профиль [4.0–5.0] с: значения > −90 дБФС на t={burst}",
              flush=True)
    return out


def main():
    results = []
    for idx in IDX:
        print(f"=== захват idx {idx} ===", flush=True)
        results.append(analyze_capture(idx))
    # сводка: модель «голова» против модели «хвост» (порог −15 дБ, канал R)
    print("\n=== сводка (канал R, порог тон−15 дБ) ===", flush=True)
    verdicts = []
    for r in results:
        b = r["channels"]["R"]["bounds_by_threshold"]["tone-15dB"]
        if b["start_s"] is None or b["end_s"] is None:
            verdicts.append({"index": r["index"], "verdict": "границы не найдены"})
            continue
        span = b["end_s"] - b["start_s"]
        head = (b["start_s"] - R_ONSET[r["index"]]) * 1000
        tail = (R_ONSET[r["index"]] + SUBMITTED_TONE_S - b["end_s"]) * 1000
        v = {"index": r["index"], "level_dbfs": None,
             "start_s": b["start_s"], "end_s": b["end_s"],
             "span_ms": round(span * 1000, 1),
             "head_loss_ms": round(head, 1), "tail_loss_ms": round(tail, 1)}
        verdicts.append(v)
        print(f"  idx {r['index']:2d}: [{b['start_s']:.3f}, {b['end_s']:.3f}] "
              f"span {span*1000:.0f} мс | голова {head:+.0f} мс, хвост {tail:+.0f} мс",
              flush=True)
    json.dump({"results": results, "summary_r_15db": verdicts},
              open(OUT / "v218-tone-bounds.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v218-tone-bounds.json")


if __name__ == "__main__":
    main()
