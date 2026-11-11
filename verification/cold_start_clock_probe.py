#!/usr/bin/env python3
"""Расследование холодного отказа clock (задание координатора, п.3).

Цикл: (1) убедиться, что устройство никто не держал ≥ IDLE_S секунд;
(2) открыть Rig, прогнать точную программу clock_check, при отказе сохранить
запись и разобрать: где тон, что в пре-окне, узкополосное отношение,
пик записи. Повторить COLD_CYCLES раз для каждого (карта, частота).
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from valvebench import measure as m, calib  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402

IDLE_S = int(os.environ.get("COLD_IDLE_S", "90"))
CYCLES = int(os.environ.get("COLD_CYCLES", "3"))
CASES = [("hw:2,0", 192000), ("hw:3,0", 96000)]  # оба показали холодный отказ однажды

results = []


def mixer_activity(hw):
    """История открытий PCM: hw_params закрыт = устройство неактивно."""
    card = hw.split(",")[0].replace("hw:", "")
    out = []
    for dev, kind in (("pcm0p", "play"), ("pcm0c", "cap")):
        p = f"/proc/asound/card{card}/{dev}/sub0/hw_params"
        try:
            state = open(p).read().split("\n")[0].strip()
        except OSError:
            state = "?"
        out.append(f"{kind}={state}")
    return " ".join(out)


def analyze(rec, fs, tone_hz=1000.0):
    n = len(rec)
    blk = int(0.005 * fs)
    mm = n // blk
    t = np.arange(n) / fs
    demod = np.exp(-2j * np.pi * tone_hz * t)
    env = np.zeros(n)
    for c in (0, 1):
        z = rec[:, c] * demod
        cs = np.concatenate(([0j], np.cumsum(z)))
        win = int(round(5 * fs / tone_hz))
        idx = np.arange(n)
        lo = np.clip(idx - win // 2, 0, n)
        hi = np.clip(idx - win // 2 + win, 0, n)
        amp = 2 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))
        env = np.maximum(env, amp)
    blocks = env[: mm * blk].reshape(mm, blk).max(axis=1)
    pre = blocks[: int(0.2 / 0.005)]
    tone_zone = blocks[int(1.0 / 0.005):int(2.0 / 0.005)]
    return {
        "pre_median": float(np.median(pre)),
        "tone_median": float(np.median(tone_zone)),
        "narrow_ratio": float(np.median(tone_zone)) / max(float(np.median(pre)), 1e-12),
        "peak": float(np.max(np.abs(rec))),
    }


for hw, rate in CASES:
    for cycle in range(1, CYCLES + 1):
        time.sleep(IDLE_S)
        status = {"case": f"{hw}@{rate}", "cycle": cycle, "idle_s": IDLE_S,
                  "hw_params_before": mixer_activity(hw)}
        try:
            cfg = calib.load()
            be = AlsaCli(hw_out=hw, hw_in=hw, in_map=cfg["in_map"], out_map=cfg["out_map"])
            rig = m.Rig(be, cfg, fs=rate, blocks=4, settle=0.25)
            rig.open()
            k, a = rig.amp_tone(1000.0, -72.0)
            seg, s_n = rig._seg([(k, a)], [(k, a)], 0.4)
            rec = be.play_record(seg)
            st = analyze(rec, rate)
            try:
                d = rig._onset(rec, tone_hz=1000.0)
                status["onset"] = f"OK@{d}"
                status["result"] = "PASS"
            except Exception as e:
                status["onset"] = f"FAIL: {type(e).__name__}"
                status["result"] = "FAIL"
            status.update(st)
            if status["result"] == "FAIL":
                np.save(f"/tmp/cold-fail-{hw.replace(':', '')}-{rate}-{cycle}.npy", rec)
        except Exception as e:
            status["result"] = f"ERROR: {type(e).__name__}: {str(e)[:80]}"
        results.append(status)
        print(json.dumps(status, ensure_ascii=False), flush=True)
        rig.close() if hasattr(rig, "close") else None
        be.close() if hasattr(be, "close") else None

json.dump(results, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "cold-start-results.json"), "w"),
          ensure_ascii=False, indent=1)
n_fail = sum(1 for r in results if r.get("result") == "FAIL")
print(f"\nИТОГ: {len(results) - n_fail}/{len(results)} PASS, отказов: {n_fail}")
