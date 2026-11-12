#!/usr/bin/env python3
"""v2.4-матрица: плато тона (production play_record) против
длительности тона × длины тишины × buffer_time. Один прогон на ячейку."""
import json
import os
import sys
import time
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.join(ROOT, "verification"))
from v24_ab_endofstream import find_card, demod_env, plateau, DAC_USBID, ADC_USBID  # noqa
from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli, SimBackend, to_pcm  # noqa: E402

FS = 192000
dac = find_card(DAC_USBID); adc = find_card(ADC_USBID)
hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
cfg = calib.Calibration()
be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc, in_map=cfg["in_map"], out_map=cfg["out_map"])
be.open(FS)
rig = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
k, a = rig.amp_tone(997.0, -30.0)
prog, s_n = rig._seg([(k, a)], [(k, a)], 0.5)

matrix = [
    (2.0, 0.0, 100), (2.0, 0.6, 100), (2.0, 1.08, 100), (2.0, 2.0, 100),
    (4.55, 0.0, 100), (4.55, 0.6, 100), (4.55, 1.08, 100),
    (4.55, 0.0, 200), (4.55, 0.0, 400),
]
rows = []
for tone_s, trail_s, buf in matrix:
    prog, _ = rig._seg([(k, a)], [(k, a)], 0.5)
    if trail_s:
        prog = np.concatenate([prog, np.zeros((int(trail_s * FS), 2))])
    pad_n = int(0.05 * FS)
    rec = be.play_record(prog.copy())
    capL, capR = rec[:, 0], rec[:, 1]
    sL, eL, _ = plateau(capL, FS)
    sR, eR, _ = plateau(capR, FS)
    pstart = max(x for x in (sL, sR) if x is not None)
    pend = min(x for x in (eL, eR) if x is not None)
    plateau_len = (pend - pstart) / FS
    submitted_tone_end_capture = pstart + 4.54  # 4.55 минус fade-in
    post0 = int((pstart + 4.55) * FS)
    r_post = 20 * np.log10(max(float(np.sqrt(np.mean(
        capR[post0:post0 + int(0.4 * FS)] ** 2)) * np.sqrt(2)), 1e-12)) \
        if post0 + int(0.4 * FS) < len(capR) else None
    rows.append({"tone_s": tone_s, "trailing_s": trail_s, "buffer_ms": buf,
                 "plateau_len_s": round(plateau_len, 4),
                 "tone_loss_s": round(4.55 - plateau_len, 4),
                 "marker": None,
                 "rms_post_dBFS_R": round(r_post, 1) if r_post else None})
    print(f"тон {tone_s:4.2f}с тишина {trail_s:4.2f}с buf {buf:3d}мс → "
          f"плато {plateau_len:6.4f} с (потеря {4.55-plateau_len:+.3f}) "
          f"RMS-после {r_post if r_post else '—'} дБФС", flush=True)
    time.sleep(0.8)
json.dump(rows, open(os.path.join(ROOT, "measurements/diagnostics/v24-matrix.json"), "w"), indent=1)
print("JSON: measurements/diagnostics/v24-matrix.json")
