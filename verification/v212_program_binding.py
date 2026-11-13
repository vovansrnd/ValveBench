#!/usr/bin/env python3
"""v2.12 — НЕЗАВИСИМАЯ привязка полного захвата к поданной программе
(пп. 3-4 ревизии v2.11).

Ключевое отличие от v2.11: привязкой служит СОХРАНЁННАЯ ПОДАННАЯ ПРОГРАММА
(её тон-сегмент), а не capture_wav, коррелированный сам с собой. Позиция
окна внутри захвата (самосопоставление capture_wav↔захват) сохраняется как
вспомогательная величина и НЕ называется program-to-capture alignment.

Анализ:
  * sha256 + пути всех трёх WAV (программа, полный захват, окно);
  * привязка: кросс-корреляция тон-сегмента программы с захватом ПО КАЖДОМУ
    каналу → seg_offset, score (независимая program-to-capture привязка);
  * точные границы тона на разрешении 2 мс (RMS-профиль всего захвата,
    порог = медиана пола + 12 дБ, устойчивость 50 мс) по каждому каналу;
  * production-окно должен лежать ВНУТРИ [t_start, t_end] — поля окон в
    миллисекундах, БЕЗ допуска в пол-LSQ-окна (125 мс);
  * потеря тона раздельно голова/хвост: t_start − seg_offset и
    (seg_offset + seg_len) − t_end — теперь ИЗМЕРЯЕМЫ раздельно, потому что
    привязка даёт абсолютное положение поданного сегмента в захвате;
  * скользящий joint-LSQ (0.25 с/шаг 0.05) ОТДЕЛЬНО по L и R с обусловленностью
    матрицы и остатком в dBFS.
"""
import hashlib
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))
from v211_full_capture_anchored import FullCaptureCtx, find_card, norm_xcorr  # noqa: E402
from v26_offline_reconstruction import F_TONE, F_PREFLIGHT  # noqa: E402
from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402

FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v212"
TAG = datetime.now().strftime("%H%M%S")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def joint_lsq_2ch(cap, fs, win_s=0.25, hop_s=0.05):
    """Скользящий joint-LSQ ОТДЕЛЬНО по каждому каналу: A997/A1000/resid
    (dBFS) + обусловленность нормированной матрицы дизайна."""
    win = int(round(win_s * fs))
    hop = int(round(hop_s * fs))
    tt = np.arange(win) / fs
    D = np.column_stack([
        np.sin(2 * np.pi * F_TONE * tt), np.cos(2 * np.pi * F_TONE * tt),
        np.sin(2 * np.pi * F_PREFLIGHT * tt), np.cos(2 * np.pi * F_PREFLIGHT * tt),
        np.ones(win)])
    Dn = D / np.linalg.norm(D, axis=0)
    sv = np.linalg.svd(Dn, compute_uv=False)
    cond = float(sv[0] / sv[-1])
    out = []
    for i in range(0, len(cap) - win, hop):
        e = {"t_center_s": round((i + win / 2) / fs, 4)}
        for c, ch in ((0, "L"), (1, "R")):
            seg = cap[i:i + win, c]
            coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
            resid = seg - D @ coef
            a997 = 20 * math.log10(max(float(np.hypot(coef[0], coef[1])), 1e-12))
            a1000 = 20 * math.log10(max(float(np.hypot(coef[2], coef[3])), 1e-12))
            r_dbfs = 20 * math.log10(max(
                float(np.sqrt(np.mean(resid ** 2)) * math.sqrt(2)), 1e-12))
            e[f"A997_{ch}"] = round(a997, 1)
            e[f"A1000_{ch}"] = round(a1000, 1)
            e[f"resid_{ch}"] = round(r_dbfs, 1)
        out.append(e)
    return out, cond


def tone_bounds_2ms(cap_ch, fs, w_ms=2.0, above_db=12.0, sustain_ms=50.0):
    """Точные границы тона на разрешении w_ms: RMS-профиль всего захвата,
    порог = медиана (пол) + above_db, устойчивость sustain_ms.
    Возвращает (t_start_s, t_end_s) или (None, None)."""
    w = int(round(w_ms * 1e-3 * fs))
    n = len(cap_ch) // w * w
    if n < w * 8:
        return None, None
    rms = np.sqrt(np.mean(cap_ch[:n].reshape(-1, w) ** 2, axis=1)) * math.sqrt(2)
    db = 20 * np.log10(np.clip(rms, 1e-12, None))
    thr = float(np.median(db)) + above_db
    run_min = max(1, int(round(sustain_ms * 1e-3 * fs / w)))
    above = db > thr
    # первый блок с устойчивым подъёмом после него
    start = end = None
    cum = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
    for i in range(len(above) - run_min):
        if above[i] and cum[i + run_min] - cum[i] >= run_min:
            start = i * w
            break
    for i in range(len(above) - 1, run_min - 1, -1):
        if above[i] and cum[i + 1] - cum[i + 1 - run_min] >= run_min:
            end = (i + 1) * w
            break
    if start is not None and end is not None and end <= start:
        return None, None
    return (start / fs if start is not None else None,
            end / fs if end is not None else None)


def main():
    dac, adc = find_card("152a:85dd"), find_card("041e:3f04")
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = calib.Calibration()
    be = AlsaCli(hw_out=f"hw:{dac},0", hw_in=f"hw:{adc},0",
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    rig = m.Rig(be, cfg, fs=FS, edge_protect=True)   # GUI-дефолты + candidate
    print(f"Rig: blocks={rig.blocks} settle={rig.settle} edge_protect="
          f"{rig.edge_protect_enabled}", flush=True)

    res = {"rig": {"blocks": rig.blocks, "settle": rig.settle,
                   "edge_protect": rig.edge_protect_enabled,
                   "lead_s": rig.edge_protect_lead_s,
                   "tail_s": rig.edge_protect_tail_s}}

    # clock_check с полным набором флагов
    for key, lvl in (("clock_72", -72.0), ("clock_30", -30.0)):
        try:
            ck = m.clock_check(rig, level_dbfs=lvl)
            res[key] = ck
            print(f"clock_check {lvl:.0f} dBFS: ok={ck.get('ok')} "
                  f"signal_ok={ck.get('signal_ok')} coherent={ck.get('coherent')} "
                  f"ppm={ {c: round(ck['ch'][c]['ppm'], 2) for c in ck['ch']} }",
                  flush=True)
        except Exception as exc:
            res[key] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            print(f"clock_check {lvl:.0f} dBFS: ИСКЛЮЧЕНИЕ {res[key]['error']}",
                  flush=True)

    # thd_point с сохранением полного захвата и программы
    win_path = OUT / f"thd-window-{TAG}.wav"
    with FullCaptureCtx(be) as ctx:
        thd = m.thd_point(rig, f=997.0, level_dbfs=-30.0, capture_wav=str(win_path))
    assert len(ctx.pairs) == 1
    prog, cap = ctx.pairs[0]
    prog_path = OUT / f"program-{TAG}.wav"
    cap_path = OUT / f"full-capture-{TAG}.wav"
    wavfile.write(prog_path, FS, prog.astype(np.float32))
    wavfile.write(cap_path, FS, cap.astype(np.float32))
    res["thd"] = thd
    print(f"thd_point: THD+N L {thd['ch']['L']['thdn_pct']:.3f} % / "
          f"R {thd['ch']['R']['thdn_pct']:.3f} %", flush=True)

    # ---- файлы: пути + sha256 (п.3 ревизии)
    res["files"] = {
        "program_wav": {"path": str(prog_path), "sha256": sha256_file(prog_path),
                        "frames": len(prog)},
        "full_capture_wav": {"path": str(cap_path), "sha256": sha256_file(cap_path),
                             "frames": len(cap)},
        "window_wav": {"path": str(win_path), "sha256": sha256_file(win_path),
                       "frames": int(round(2.0 * FS))},
    }

    # ---- НЕЗАВИСИМАЯ привязка: тон-сегмент ПОДАННОЙ программы в захвате
    lead_n = int(round(rig.edge_protect_lead_s * FS))
    seg_len = int(round((rig.settle + rig.blocks * rig.n / FS + 0.05) * FS))
    submitted_tone_s = seg_len / FS
    seg_tmpl = prog[lead_n:lead_n + seg_len]
    binding = {"seg_len_frames": seg_len,
               "submitted_tone_span_s": round(submitted_tone_s, 4)}
    for c, ch in ((0, "L"), (1, "R")):
        off, score = norm_xcorr(cap[:, c], seg_tmpl[:, c])
        binding[f"seg_offset_{ch}_frames"] = off
        binding[f"seg_offset_{ch}_s"] = round(off / FS, 5)
        binding[f"score_{ch}"] = round(score, 4)
    res["program_binding"] = binding
    print(f"привязка программы: offset L {binding['seg_offset_L_s']} с "
          f"(score {binding['score_L']}), R {binding['seg_offset_R_s']} с "
          f"(score {binding['score_R']})", flush=True)

    # ---- позиция окна в захвате (самосопоставление — вспомогательная)
    _, win_wav = wavfile.read(str(win_path))
    win_wav = win_wav.astype(np.float64)
    woff, wscore = norm_xcorr(cap[:, 1], win_wav[:, 1])
    res["window_position"] = {
        "note": ("самосопоставление capture_wav в захвате: точная позиция "
                 "окна внутри записи, НЕ program-to-capture alignment"),
        "offset_frames": woff, "start_s": round(woff / FS, 5),
        "end_s": round((woff + len(win_wav)) / FS, 5),
        "score": round(wscore, 4)}

    # ---- точные границы тона 2 мс по каналам + поля окна + потери
    bounds = {}
    for c, ch in ((0, "L"), (1, "R")):
        t0, t1 = tone_bounds_2ms(cap[:, c], FS)
        seg_off = binding[f"seg_offset_{ch}_frames"] / FS
        w0 = woff / FS
        w1 = (woff + len(win_wav)) / FS
        rec = {"tone_start_s": round(t0, 5) if t0 is not None else None,
               "tone_end_s": round(t1, 5) if t1 is not None else None,
               "window_start_s": round(w0, 5), "window_end_s": round(w1, 5)}
        if t0 is not None and t1 is not None:
            rec["window_start_margin_ms"] = round((w0 - t0) * 1000, 2)
            rec["window_end_margin_ms"] = round((t1 - w1) * 1000, 2)
            rec["window_inside_tone_2ms"] = bool(w0 >= t0 and w1 <= t1)
            rec["head_loss_ms"] = round((t0 - seg_off) * 1000, 2)
            rec["tail_loss_ms"] = round((seg_off + submitted_tone_s - t1) * 1000, 2)
            rec["tone_span_ms"] = round((t1 - t0) * 1000, 2)
        bounds[ch] = rec
        print(f"{ch}: тон {rec['tone_start_s']}..{rec['tone_end_s']} с "
              f"({rec.get('tone_span_ms')} мс из поданных {submitted_tone_s*1000:.0f} мс) | "
              f"голова {rec.get('head_loss_ms')} мс, хвост {rec.get('tail_loss_ms')} мс | "
              f"окно внутри: {rec.get('window_inside_tone_2ms')} "
              f"(поля {rec.get('window_start_margin_ms')}/"
              f"{rec.get('window_end_margin_ms')} мс)", flush=True)
    res["tone_bounds_2ms"] = bounds

    # ---- joint-LSQ по обоим каналам
    prof, cond = joint_lsq_2ch(cap, FS)
    lszones = {}
    for ch in ("L", "R"):
        stable = [p for p in prof
                  if p[f"A997_{ch}"] > p[f"A1000_{ch}"] + 6 and p[f"A997_{ch}"] > -45]
        lszones[ch] = {"stable997_windows": len(stable),
                       "windows_total": len(prof),
                       "first_center_s": stable[0]["t_center_s"] if stable else None,
                       "last_center_s": stable[-1]["t_center_s"] if stable else None,
                       "min_A997": min((p[f"A997_{ch}"] for p in stable), default=None),
                       "max_resid": max((p[f"resid_{ch}"] for p in stable), default=None)}
        print(f"LSQ {ch}: stable997 {lszones[ch]['stable997_windows']}/"
              f"{len(prof)} окон, min A997 {lszones[ch]['min_A997']} дБФС, "
              f"max resid {lszones[ch]['max_resid']} дБФС", flush=True)
    res["lsq_2ch"] = {"cond_number": round(cond, 2),
                      "win_s": 0.25, "hop_s": 0.05, "zones": lszones}

    json.dump(res, open(OUT / f"v212-results-{TAG}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("\nJSON:", OUT / f"v212-results-{TAG}.json")


if __name__ == "__main__":
    main()
