#!/usr/bin/env python3
"""v2.15 — офлайн-аудит НЕУДАЧНЫХ clock-захватов v2.14. НИКАКОГО playback.

Вопрос координатора: сигнал −72/−66 действительно неразличим на ОБОИХ
каналах, или шумный L маскирует пригодный сигнал R в объединённом
onset-детекторе (production env = max(L, R))?

Метод: ТОЧНОЕ воспроизведение алгоритма Rig._onset_narrowband с
production-константами (блоки 5 мс, noise = медиана первых 0.2 с, thr =
max(8·noise, 0.1·p99), sustain 90% за 1 с) — отдельно для L, отдельно для R
и для объединённой огибающей (как в production). Никаких новых порогов.

Дополнительно: LSQ-амплитуда 1 кГц и локальный шум в ОЖИДАЕМОМ окне
измерения (2.255–4.255 с — ориентир успешных захватов, onset ≈ 356160
кадров), разбор первых 0.2 с (что попало в оценку шума), поиск устойчивого
участка по всей записи, сравнение с успешными −60/−54 dBFS.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

BASE = Path(__file__).resolve().parents[2]        # .../measurements
SRC = BASE / "diagnostics" / "v214-clock-diagnostic"
SRC_JSON = SRC / "20261114-004425-results.json"
OUT = BASE / "diagnostics" / "v215-clock-failed-capture-audit"
FS = 192000
TONE_HZ = 1000.0          # clock pilot — точно 1 кГц (df=1)
EXP_ONSET_FRAMES = 356160 # ориентир успешных захватов (координатор)
EXP_WINDOW = (2.255, 4.255)   # onset + settle 0.4 → +2.0 блоков
ANALYZED = [1, 2, 15, 16]             # неудачные: −72 asc, −66 asc, −66 desc, −72 desc
REFERENCES = [3, 4, 8]                # успешные: −60 asc, −54 asc, −30 asc


def narrowband_blocks(rec_ch, fs, tone_hz):
    """Огибающая 5-мс блоков одного канала — ТОЧНО как в
    Rig._onset_narrowband (демодуляция, окно 5 периодов, max по 5-мс)."""
    n = len(rec_ch)
    t = np.arange(n) / fs
    z = rec_ch * np.exp(-2j * np.pi * tone_hz * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(5 * fs / tone_hz)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    amp = 2.0 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))
    blk = int(0.005 * fs)
    m = n // blk
    return amp[: m * blk].reshape(m, blk).max(axis=1)


def detector_stats(blocks):
    """Production-константы без изменений: noise/peak/thr/доля выше/sustain."""
    noise = max(float(np.median(blocks[: int(0.2 / 0.005)])), 1e-7)
    peak = float(np.percentile(blocks, 99))
    thr = max(8.0 * noise, 0.1 * peak)
    above = blocks > thr
    frac_above = float(np.mean(above))
    sustain = max(4, min(int(1.0 / 0.005), len(blocks)))
    cum = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
    onset = None
    for i in range(len(blocks) - 4):
        end = min(len(blocks), i + sustain)
        span = end - i
        if above[i] and (cum[end] - cum[i]) >= 0.9 * span:
            onset = i * int(0.005 * FS)
            break
    db = 20 * np.log10(np.clip(blocks, 1e-12, None))
    return {
        "noise_dbfs": round(20 * math.log10(noise), 1),
        "peak99_dbfs": round(20 * math.log10(max(peak, 1e-12)), 1),
        "thr_dbfs": round(20 * math.log10(thr), 1),
        "thr_rule": ("8x_noise" if 8.0 * noise >= 0.1 * peak else "0.1x_peak99"),
        "frac_blocks_above_thr": round(frac_above, 4),
        "sustain_found": onset is not None,
        "onset_frame": onset,
        "onset_s": round(onset / FS, 4) if onset is not None else None,
        "blocks_median_dbfs": round(float(np.median(db)), 1),
        "blocks_p90_dbfs": round(float(np.percentile(db, 90)), 1),
    }


def lsq_1k(rec_ch, fs, t0, t1, f=TONE_HZ):
    """Амплитуда 1 кГц и локальный шум в окне [t0, t1] (сек)."""
    a, b = int(t0 * fs), int(t1 * fs)
    seg = rec_ch[a:b]
    tt = np.arange(len(seg)) / fs
    D = np.column_stack([np.sin(2 * np.pi * f * tt),
                         np.cos(2 * np.pi * f * tt), np.ones(len(seg))])
    coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
    resid = seg - D @ coef
    amp = float(np.hypot(coef[0], coef[1]))
    noise = float(np.sqrt(np.mean(resid ** 2)) * math.sqrt(2))
    return {
        "A1k_dbfs": round(20 * math.log10(max(amp, 1e-12)), 1),
        "local_noise_dbfs": round(20 * math.log10(max(noise, 1e-12)), 1),
        "snr_db": round(20 * math.log10(max(amp, 1e-12)) -
                        20 * math.log10(max(noise, 1e-12)), 1),
    }


def analyze_capture(path, fs=FS):
    _, rec = wavfile.read(path)
    rec = rec.astype(np.float64)
    n = len(rec)
    blk = int(0.005 * fs)
    m = n // blk
    # поканальные огибающие
    blocks_L = narrowband_blocks(rec[:, 0], fs, TONE_HZ)
    blocks_R = narrowband_blocks(rec[:, 1], fs, TONE_HZ)
    # объединённая — как production: max каналов ПОКАДРОВО
    blocks_comb = np.maximum(blocks_L, blocks_R)
    res = {
        "capture": Path(path).name,
        "frames": n,
        "production_reproduction": {
            "combined": detector_stats(blocks_comb),
            "L": detector_stats(blocks_L),
            "R": detector_stats(blocks_R),
        },
        "expected_window": {"t0_s": EXP_WINDOW[0], "t1_s": EXP_WINDOW[1]},
        "first_02s_blocks_dbfs": {
            "L": {"median": round(20 * math.log10(max(float(np.median(blocks_L[:40])), 1e-12)), 1),
                  "max": round(20 * math.log10(max(float(np.max(blocks_L[:40])), 1e-12)), 1)},
            "R": {"median": round(20 * math.log10(max(float(np.median(blocks_R[:40])), 1e-12)), 1),
                  "max": round(20 * math.log10(max(float(np.max(blocks_R[:40])), 1e-12)), 1)},
        },
    }
    # тон в ожидаемом окне: демод-огибающая (медиана блоков окна) + LSQ
    i0, i1 = int(EXP_WINDOW[0] * fs / blk), int(EXP_WINDOW[1] * fs / blk)
    for ch, bl in (("L", blocks_L), ("R", blocks_R)):
        seg = bl[i0:i1]
        res.setdefault("window_demod_env_dbfs", {})[ch] = round(
            20 * math.log10(max(float(np.median(seg)), 1e-12)), 1)
    res["window_lsq_1k"] = {
        "L": lsq_1k(rec[:, 0], fs, *EXP_WINDOW),
        "R": lsq_1k(rec[:, 1], fs, *EXP_WINDOW),
    }
    # устойчивый участок по всей записи при ПОРАНЕШНЕМ старте отсчёта шума:
    # те же production-константы, но шум оценивается по самому тихому
    # 0.2-с участку записи (не новые пороги — та же формула, другой источник
    # оценки шума; помечено явно как диагностика)
    diag = {}
    for ch, bl in (("L", blocks_L), ("R", blocks_R)):
        w = 40
        med = np.array([np.median(bl[i:i + w]) for i in range(0, len(bl) - w, w)])
        quiet_noise = max(float(np.min(med)), 1e-7)
        peak = float(np.percentile(bl, 99))
        thr = max(8.0 * quiet_noise, 0.1 * peak)
        above = bl > thr
        sustain = max(4, min(int(1.0 / 0.005), len(bl)))
        cum = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
        onset = None
        for i in range(len(bl) - 4):
            end = min(len(bl), i + sustain)
            if above[i] and (cum[end] - cum[i]) >= 0.9 * (end - i):
                onset = i * blk
                break
        diag[ch] = {
            "quietest_02s_noise_dbfs": round(20 * math.log10(quiet_noise), 1),
            "thr_with_quiet_noise_dbfs": round(20 * math.log10(thr), 1),
            "sustain_found_whole_record": onset is not None,
            "onset_s": round(onset / FS, 4) if onset is not None else None,
        }
    res["diagnostic_quiet_noise_reference"] = {
        "note": ("те же production-константы (8x/0.1x/90%), но шум — медиана "
                 "самого тихого 0.2-с участка записи; диагностика, не production"),
        "channels": diag}
    return res


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    src = json.load(open(SRC_JSON))
    results = []
    print("=== v2.15: офлайн-аудит неудачных clock-захватов (без playback) ===\n",
          flush=True)
    for idx in ANALYZED + REFERENCES:
        r = next(x for x in src["clock_results"] if x["index"] == idx)
        path = SRC / Path(r["files"]["capture"]["path"]).name
        failed = r.get("clock_error") is not None
        rec = analyze_capture(str(path))
        rec_out = {"index": idx, "level_dbfs": r["level_dbfs"],
                   "production_failed": failed,
                   "production_error": (r.get("clock_error") or "")[:90]}
        rec_out.update(rec)
        results.append(rec_out)
        pr = rec["production_reproduction"]
        w = rec["window_lsq_1k"]
        tag = "FAIL" if failed else "ok  "
        print(f"[{idx:02d}] {r['level_dbfs']:+6.0f} dBFS {tag}: "
              f"combined thr {pr['combined']['thr_dbfs']} дБФС "
              f"({pr['combined']['thr_rule']}), sustain "
              f"{pr['combined']['sustain_found']}", flush=True)
        for ch in ("L", "R"):
            p = pr[ch]
            print(f"     {ch}: noise {p['noise_dbfs']} peak99 {p['peak99_dbfs']} "
                  f"thr {p['thr_dbfs']} выше-порога {p['frac_blocks_above_thr']:.2%} "
                  f"sustain {p['sustain_found']} | "
                  f"A1k {w[ch]['A1k_dbfs']} шум {w[ch]['local_noise_dbfs']} "
                  f"SNR {w[ch]['snr_db']} дБ", flush=True)
    json.dump({"source_json": str(SRC_JSON),
               "production_constants": {"block_ms": 5, "noise_window_s": 0.2,
                                        "thr": "max(8x_noise, 0.1x_p99)",
                                        "sustain": "90% за 1 с"},
               "expected_window": EXP_WINDOW,
               "results": results},
              open(OUT / "v215-audit-results.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v215-audit-results.json")


if __name__ == "__main__":
    main()
