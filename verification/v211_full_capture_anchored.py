#!/usr/bin/env python3
"""v2.11 — ОДИН полный диагностический захват с привязкой к поданной
программе (п.5 ревизии v2.10) + ПОЛНЫЙ набор флагов clock_check (п.3).

Production-путь целиком: GUI-дефолты Rig (blocks=2, settle=0.5) + candidate
edge-защита (явно включена — v2.11: по умолчанию ВЫКЛЮЧЕНА). play_record
обёрнут: ПОЛНЫЙ захват и ПОЛНАЯ поданная программа сохраняются на диск.

Анализ: скользящий joint-LSQ (0.25 с/шаг 0.05) по полному захвату → границы
устойчивого 997 Гц; позиция production-окна (capture_wav = само окно) в
полном захвате через нормированную кросс-корреляцию; потеря тона = поданные
2.55 с (settle 0.5 + 2 блока + запас 0.05) минус устойчивый участок.
Абсолютное разделение голова/хвост невозможно (старт aplay внутри захвата
неизвестен — оговорка v2.6) — фиксируем суммарную потерю и покрытие окна.

Масштаб: один прогон, не большая аппаратная кампания (ревизия v2.10).
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))
from v26_offline_reconstruction import joint_lsq_profile  # noqa: E402
from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402

DAC_USBID = "152a:85dd"
ADC_USBID = "041e:3f04"
FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v211"
TAG = datetime.now().strftime("%H%M%S")


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


def norm_xcorr(arr, tmpl):
    """(best_offset, score) нормированной кросс-корреляции tmpl в arr.
    Энергия окна — скользящая сумма через cumsum (O(n)); np.convolve был
    O(n·m) и на полном захвате считал минуты."""
    n, mm = len(arr), len(tmpl)
    L = int(2 ** np.ceil(np.log2(n + mm)))
    F = np.fft.rfft(arr, L) * np.conj(np.fft.rfft(tmpl, L))
    cc = np.fft.irfft(F, L)[: n - mm + 1]
    cs = np.concatenate(([0.0], np.cumsum(arr ** 2)))
    energy = (cs[mm:] - cs[: n - mm + 1]).clip(1e-30)
    cc = cc / (np.linalg.norm(tmpl) * np.sqrt(energy))
    return int(np.argmax(cc)), float(np.max(cc))


class FullCaptureCtx:
    """Обёртка play_record: сохраняет КАЖДУЮ поданную программу и полный
    захват production-вызова (привязка программа↔захват, п.5 ревизии)."""

    def __init__(self, be):
        self.be = be
        self.pairs = []
        self._orig = be.play_record

    def __enter__(self):
        ctx = self

        def wrapped(out, *a, **kw):
            rec = ctx._orig(out, *a, **kw)
            ctx.pairs.append((np.asarray(out, dtype=np.float64).copy(),
                              np.asarray(rec, dtype=np.float64).copy()))
            return rec

        self.be.play_record = wrapped
        return self

    def __exit__(self, *a):
        self.be.play_record = self._orig


def main():
    dac, adc = find_card(DAC_USBID), find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = calib.Calibration()
    be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc,
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    rig = m.Rig(be, cfg, fs=FS, edge_protect=True)   # GUI-дефолты + candidate
    print(f"Rig: blocks={rig.blocks} settle={rig.settle} edge_protect="
          f"{rig.edge_protect_enabled} lead={rig.edge_protect_lead_s} "
          f"tail={rig.edge_protect_tail_s}", flush=True)

    res = {"rig": {"blocks": rig.blocks, "settle": rig.settle,
                   "edge_protect": rig.edge_protect_enabled,
                   "lead_s": rig.edge_protect_lead_s,
                   "tail_s": rig.edge_protect_tail_s}}

    # 1-2) clock_check с ПОЛНЫМ набором флагов в JSON (п.3 ревизии)
    for key, lvl in (("clock_72", -72.0), ("clock_30", -30.0)):
        try:
            ck = m.clock_check(rig, level_dbfs=lvl)
            res[key] = ck   # весь dict: signal_ok/clock_ok/clock_consistent/
            # coherent/ok раздельно, snr_db/local_noise_dbfs/coh_err по каналам
            print(f"clock_check {lvl:.0f} dBFS: ok={ck.get('ok')} "
                  f"signal_ok={ck.get('signal_ok')} "
                  f"clock_ok={ck.get('clock_ok')} coherent={ck.get('coherent')} "
                  f"ppm={ {c: round(ck['ch'][c]['ppm'], 2) for c in ck['ch']} } "
                  f"snr={ {c: round(ck['ch'][c]['snr_db'], 1) for c in ck['ch']} }",
                  flush=True)
        except Exception as exc:
            res[key] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            print(f"clock_check {lvl:.0f} dBFS: ИСКЛЮЧЕНИЕ {res[key]['error']}",
                  flush=True)

    # 3) thd_point с сохранением ПОЛНОГО захвата и программы
    win_path = OUT / f"thd-window-{TAG}.wav"
    with FullCaptureCtx(be) as ctx:
        thd = m.thd_point(rig, f=997.0, level_dbfs=-30.0, capture_wav=str(win_path))
    assert len(ctx.pairs) == 1, f"ожидался 1 play_record, было {len(ctx.pairs)}"
    prog, cap = ctx.pairs[0]
    prog_path = OUT / f"program-{TAG}.wav"
    cap_path = OUT / f"full-capture-{TAG}.wav"
    wavfile.write(prog_path, FS, prog.astype(np.float32))
    wavfile.write(cap_path, FS, cap.astype(np.float32))
    res["thd"] = thd
    print(f"thd_point: THD+N L {thd['ch']['L']['thdn_pct']:.3f} % / "
          f"R {thd['ch']['R']['thdn_pct']:.3f} % | программа {len(prog)/FS:.2f} с, "
          f"захват {len(cap)/FS:.2f} с", flush=True)

    # 4) анализ полного захвата: устойчивый 997, позиция окна, потеря
    _, win_wav = wavfile.read(str(win_path))
    win_wav = win_wav.astype(np.float64)
    off, score = norm_xcorr(cap[:, 1], win_wav[:, 1])
    prof = joint_lsq_profile(cap, FS, 0, len(cap), win_s=0.25, hop_s=0.05)
    stable = [p for p in prof
              if p["A_997_dbfs"] > p["A_1000_dbfs"] + 6 and p["A_997_dbfs"] > -45]
    submitted_tone_s = rig.settle + rig.blocks * rig.n / FS + 0.05   # 2.55 с
    rec = {
        "submitted_program_s": round(len(prog) / FS, 3),
        "captured_s": round(len(cap) / FS, 3),
        "submitted_tone_span_s": round(submitted_tone_s, 3),
        "stable997_first_s": stable[0]["t_center_s"] if stable else None,
        "stable997_last_s": stable[-1]["t_center_s"] if stable else None,
        "stable997_span_s": round(stable[-1]["t_center_s"] - stable[0]["t_center_s"], 3)
        if stable else 0,
        "stable997_windows": len(stable),
        "lsq_windows_total": len(prof),
        "min_A997_in_stable": min((p["A_997_dbfs"] for p in stable), default=None),
        "window_offset_frames": off,
        "window_offset_s": round(off / FS, 4),
        "window_xcorr_score": round(score, 4),
        "window_start_s": round(off / FS, 4),
        "window_end_s": round((off + len(win_wav)) / FS, 4),
    }
    if stable:
        # покрытие окна устойчивой зоной (допуск = разрешение LSQ 0.25 с)
        margin = 0.125
        rec["window_inside_stable"] = bool(
            off / FS >= stable[0]["t_center_s"] - margin and
            (off + len(win_wav)) / FS <= stable[-1]["t_center_s"] + margin)
        rec["tone_loss_s"] = round(submitted_tone_s - rec["stable997_span_s"], 3)
        # head-ориентир: первая вспышка над полом (щелчок/остаток D10) —
        # прокси старта потока; абсолютное разделение голова/хвост невозможно
        blk = int(0.005 * FS)
        mm = len(cap) // blk
        env = np.sqrt(np.mean(cap[:mm * blk, :].reshape(mm, blk, 2) ** 2,
                              axis=1)).max(axis=1)
        floor = float(np.median(env[:int(0.2 / 0.005)]))
        hi = np.where(env > max(floor * 8, 1e-7))[0]
        rec["stream_activity_first_s"] = round(hi[0] * blk / FS, 4) if len(hi) else None
        rec["head_split_caveat"] = ("старт aplay внутри захвата неизвестен — "
                                    "суммарная потеря тона и покрытие окна "
                                    "фиксируются как основные величины")
    res["full_capture_analysis"] = rec
    print(f"stable997: {rec['stable997_first_s']}..{rec['stable997_last_s']} "
          f"({rec['stable997_span_s']} с из {rec['submitted_tone_span_s']} поданных) "
          f"| потеря {rec.get('tone_loss_s')} с", flush=True)
    print(f"окно production: {rec['window_start_s']}..{rec['window_end_s']} с "
          f"(xcorr {rec['window_xcorr_score']}) | внутри устойчивой зоны: "
          f"{rec.get('window_inside_stable')}", flush=True)

    json.dump(res, open(OUT / f"v211-results-{TAG}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("\nJSON:", OUT / f"v211-results-{TAG}.json")


if __name__ == "__main__":
    main()
