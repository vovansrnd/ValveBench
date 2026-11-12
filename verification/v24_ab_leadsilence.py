#!/usr/bin/env python3
"""v2.4-A/B с лидирующей тишиной: программа начинается с 1.5 с цифровой
тишины (сбрасывает цикл-повтор D10 в тишину до старта тона), затем тон
4.55 с. A: тон до конца программы. B: тон + 0.6 с тишины.
Production play_record; по 2 повтора; всё в measurements/diagnostics/v24-ab/."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))
from v24_ab_endofstream import find_card, demod_env, DAC_USBID, ADC_USBID  # noqa: E402
from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli, SimBackend  # noqa: E402

FS = 192000
dac = find_card(DAC_USBID); adc = find_card(ADC_USBID)
hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
cfg = calib.Calibration()
be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc, in_map=cfg["in_map"], out_map=cfg["out_map"])
be.open(FS)
rig = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
k, a = rig.amp_tone(997.0, -30.0)
prog, s_n = rig._seg([(k, a)], [(k, a)], 0.5)   # 4.55 с непрерывного тона

OUT = ROOT / "measurements" / "diagnostics" / "v24-ab"
OUT.mkdir(exist_ok=True)
results = []
for vname, trailing in (("A", 0.0), ("A", 0.0), ("B", 0.6), ("B", 0.6)):
    lead = np.zeros((int(1.5 * FS), 2))
    arr = np.concatenate([lead, prog, np.zeros((int(trailing * FS), 2))])
    rec = be.play_record(arr.copy())
    capL, capR = rec[:, 0], rec[:, 1]
    sL, eL, _ = demod_plateau = None, None, None
    # плато по огибающей 997 (−45 dBFS, устойчивость 30 мс), оба канала
    eL = demod_env(capL, FS, 997)
    eR = demod_env(capR, FS, 997)
    def bounds(e):
        above = np.where(e > 10 ** (-45 / 20))[0]
        run_min = int(0.03 * FS)
        st = en = None
        if len(above):
            for i in above:
                if (e[i:i + run_min] > 10 ** (-45 / 20)).all():
                    st = i; break
            for i in above[::-1]:
                if (e[max(0, i - run_min + 1):i + 1] > 10 ** (-45 / 20)).all():
                    en = i + 1; break
        return st, en
    sL, eL = bounds(eL)
    sR, eR = bounds(eR)
    pstart = max(sL, sR); pend = min(eL, eR)
    plateau_len = (pend - pstart) / FS
    # ожидаемый конец тона: старт плато = конец lead + fade-in → тон до
    # (pstart + 4.54 с); тишина в хвосте программы
    expected_end = pstart + int(4.54 * FS)
    trunc = (pend - expected_end) / FS
    # RMS послетоновая зона: [pstart+4.55 .. +5.15] — тишина программы
    post0 = pstart + int(4.55 * FS)
    post = 20 * np.log10(max(float(np.sqrt(np.mean(
        capR[post0:post0 + int(0.4 * FS)] ** 2)) * np.sqrt(2)), 1e-12)) \
        if post0 + int(0.4 * FS) < len(capR) else None
    # RMS в последние 0.3 с плато (до обрыва): тон или уже деградация?
    pre_end = 20 * np.log10(max(float(np.sqrt(np.mean(
        capR[pend - int(0.3 * FS):pend]**2)) * np.sqrt(2)), 1e-12))
    r = {"variant": vname, "repeat": len([x for x in results if x["variant"] == vname]) + 1,
         "plateau_start_s": round(pstart / FS, 4),
         "plateau_len_s": round(plateau_len, 4),
         "expected_full_tone_s": 4.55,
         "tone_loss_s": round(4.55 - plateau_len, 4),
         "trunc_vs_expected_end_s": round(trunc, 4),
         "rms_last_0_3s_of_plateau_dBFS": round(pre_end, 1),
         "rms_post_tone_0_4s_dBFS": round(post, 1) if post else None}
    results.append(r)
    print(f"[{vname}{r['repeat']}] плато {plateau_len:.4f} с (подано 4.55) | "
          f"усечение vs ожидаемый конец: {trunc:+.3f} с | RMS хвост плато {pre_end} | "
          f"RMS послетон 0.4с {post}", flush=True)
    np.save(OUT / f"v24ls_capture_{vname}{r['repeat']}.npy", rec.astype(np.float32))
json.dump(results, open(OUT / "v24-leadsilence-summary.json", "w"), indent=1)
print("JSON:", OUT / "v24-leadsilence-summary.json")
