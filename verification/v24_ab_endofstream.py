#!/usr/bin/env python3
"""v2.4 — A/B-тест end-of-stream через НАСТОЯЩИЙ AlsaCli.play_record (§ задания).

A: программа THD (Rig._seg, 4.55 с непрерывного тона 997 Гц −30 dBFS с фейдами
   Rig._seg) заканчивается последним сегментом — только штатные 50 мс пада
   AlsaCli.play_record.
B: тот же тон + 0.6 с цифровой тишины ВНУТРИ массива, передаваемого
   play_record (до штатного пада). tail play_record одинаков (0.8 с, доп.
   запись, не тишина в playback).

Production-код не изменяется. Каждая пара прогоняется дважды. Все артефакты —
в measurements/diagnostics/v24-ab/ (JSON/текст в git; крупные массивы на диске).
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli, SimBackend, to_pcm  # noqa: E402

DAC_USBID = "152a:85dd"   # SMSL D10
ADC_USBID = "041e:3f04"   # E-MU 0404
FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v24-ab"


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


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


def plateau(arr, fs, thr_db=-45.0, sustain_ms=30):
    e = demod_env(arr, fs, 997)
    thr = 10 ** (thr_db / 20)
    above = e > thr
    run_min = int(round(sustain_ms * fs / 1000))
    idx = np.where(above)[0]
    start = end = None
    if len(idx):
        for i in idx:
            if above[i:i + run_min].all():
                start = i
                break
        for i in idx[::-1]:
            if above[max(0, i - run_min + 1):i + 1].all():
                end = i + 1
                break
    return start, end, e


def main():
    dac = find_card(DAC_USBID); adc = find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
    print(f"D10 = {hw_dac} | E-MU = {hw_adc}")
    OUT.mkdir(parents=True, exist_ok=True)

    cfg = calib.Calibration()
    be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc,
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    print(f"фактически: fmt_out={be.fmt_out} fmt_in={be.fmt_in} "
          f"ch_out={be.ch_out} ch_in={be.ch_in} buffer_ms={be.buffer_ms} "
          f"hw={be.hw}")

    # программа — ровно production: Rig._seg (SimBackend, железо не трогается)
    rig = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
    k, a = rig.amp_tone(997.0, -30.0)
    prog, s_n = rig._seg([(k, a)], [(k, a)], 0.5)  # как в THD-прогоне координатора
    print(f"программа Rig._seg: {len(prog)} кадров ({len(prog)/FS:.3f} с), "
          f"settle s_n={s_n} ({s_n/FS:.3f} с) — тон с сэмпла 0, весь сегмент")

    post_silence = np.zeros((int(0.6 * FS), 2))
    variants = {
        "A": prog,
        "B": np.concatenate([prog, post_silence]),
    }

    pad_n = int(0.05 * FS)
    results = []
    for vname, arr in variants.items():
        for rep in (1, 2):
            tag = f"{vname}{rep}"
            pcm_sent = to_pcm(np.vstack([np.zeros((pad_n, 2)), arr,
                                         np.zeros((pad_n, 2))]), be.fmt_out)
            pcm_bytes = pcm_sent if isinstance(pcm_sent, (bytes, bytearray))                 else pcm_sent.tobytes()
            t0 = time.monotonic()
            rec = be.play_record(arr.copy())
            elapsed = round(time.monotonic() - t0, 3)
            np.save(OUT / f"playback_source_{tag}.npy", arr.astype(np.float32))
            (OUT / f"pcm_sent_{tag}.bin").write_bytes(pcm_bytes)
            np.save(OUT / f"capture_full_{tag}.npy", rec.astype(np.float32))
            with open(OUT / f"commands_{tag}.txt", "w") as fh:
                fh.write("arecord: " + " ".join(be._cmd("arecord", "c")) +
                         f" -d {int((len(arr)/FS + 0.1 + 0.45 + 1))}\n")
                fh.write("aplay:   " + " ".join(be._cmd("aplay", "p")) + " (stdin)\n")
                fh.write(f"stdin bytes: {len(pcm_bytes)}\n")
                fh.write(f"aplay/arecord rc: 0/0 (play_record raises BackendError "
                         f"при ненулевом rc — исключения не было)\n")
                fh.write(f"be.notes: {be.notes}\n")
                fh.write(f"elapsed_s: {elapsed}\n")

            # --- анализ
            capL, capR = rec[:, 0], rec[:, 1]
            sL, eL, _ = plateau(capL, FS)
            sR, eR, _ = plateau(capR, FS)
            plateau_start = max(x for x in (sL, sR) if x is not None)
            expected_tone_end = plateau_start + int(4.54 * FS)  # 4.55 − fade-in 0.01
            expected_end_s = expected_tone_end / FS
            loss = {}
            for name, arrc in (("L", capL), ("R", capR)):
                e = demod_env(arrc, FS, 997)
                above = np.where(e > 10 ** (-45 / 20))[0]
                above = above[above < len(arrc) - int(0.2 * FS)]  # исключить хвост-мусор
                last45 = int(above[-1]) if len(above) else None
                loss[f"last_above45_{name}"] = last45
                loss[f"loss_s_{name}"] = round(
                    (expected_tone_end - last45) / FS, 4) if last45 else None
            # RMS: последняя секунда тона vs зона после поданного конца тона
            submitted_end = plateau_start + int((4.55 + 0.05) * FS) - int(0.01 * FS)
            post_zone0 = submitted_end
            post_zone1 = min(len(capL) - int(0.2 * FS), submitted_end + int(0.8 * FS))
            r_post = {}
            for name, arrc in (("L", capL), ("R", capR)):
                r_tone = 20 * np.log10(max(float(np.sqrt(np.mean(
                    arrc[plateau_start + int(0.2*FS):plateau_start + int(1.2*FS)]**2))
                    * np.sqrt(2)), 1e-12))
                r_post[name] = round(20 * np.log10(max(float(np.sqrt(np.mean(
                    arrc[post_zone0:post_zone1]**2)) * np.sqrt(2)), 1e-12)), 1) \
                    if post_zone1 > post_zone0 else None
                r_post[f"rms_tone_{name}"] = round(r_tone, 1)
            rec_res = {
                "variant": vname, "repeat": rep,
                "submitted_tone_s": 4.55,
                "in_array_post_silence_s": 0.6 if vname == "B" else 0.0,
                "captured_frames": len(rec),
                "captured_s": round(len(rec) / FS, 3),
                "plateau_start_s": round(plateau_start / FS, 4),
                "plateau_len_s": round((eR if eR else eL) / FS - plateau_start / FS, 4),
                "expected_tone_end_s": round(expected_end_s, 4),
                **loss,
                "rms_post_zone_dBFS": r_post,
                "elapsed_s": elapsed,
            }
            results.append(rec_res)
            print(f"[{tag}] плато {rec_res['plateau_start_s']:.3f}+{rec_res['plateau_len_s']}с "
                  f"| ожид. конец {expected_end_s:.3f} | "
                  f"посл. −45дБ: L={loss['last_above45_L']} R={loss['last_above45_R']} "
                  f"| потеря L={loss['loss_s_L']} R={loss['loss_s_R']} | "
                  f"RMS после конца: L={r_post['L']} R={r_post['R']}", flush=True)
            time.sleep(1.0)
    
    print("\n=== СВОДКА A/B ===")
    for r in results:
        print(f"{r['variant']}{r['repeat']}: плато {r['plateau_len_s']} с "
              f"(подано 4.55) | потеря L {r['loss_s_L']} R {r['loss_s_R']} | "
              f"RMS после конца {r['rms_post_zone_dBFS']['L']}/{r['rms_post_zone_dBFS']['R']} дБФС")
    json.dump({"fs": FS, "fmt_out": be.fmt_out, "fmt_in": be.fmt_in,
               "buffer_ms": be.buffer_ms, "results": results,
               "timestamp": datetime.now().isoformat()},
              open(OUT / "v24-summary.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v24-summary.json")


if __name__ == "__main__":
    main()
