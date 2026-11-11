#!/usr/bin/env python3
"""Аппаратная валидация Gain Match на E-MU loopback: известные сдвиги +0.5/+1.0 дБ.

Базовая ΔG тракта измеряется первой (стабильна, подтверждено 5 повторами),
затем измеряются известные цифровые сдвиги — разница покажет точность прибора.
"""
import math
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, "/opt/MyApps/Adudio/for_z/audit-0120/ValveBench-0.1.20-dev3")
from valvebench import gainmatch
from valvebench.measure import Rig
from valvebench.calib import Calibration
from valvebench.backend import Backend

EMU = "hw:3,0"
FS = 48000
LEVEL = -6.0
RES = []


def check(name, ok, detail=""):
    RES.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("\n      " + detail if detail else ""))


class EmuHw(Backend):
    name = "alsa-hw"

    def __init__(self):
        self.hw = EMU
        self.notes = []
        self.fs = FS

    def open(self, fs):
        self.fs = int(fs)

    def play_record(self, out, tail=0.8):
        out = np.asarray(out, float)
        if out.ndim == 1:
            out = np.stack([out, out], axis=1)
        rec = subprocess.Popen(["arecord", "-D", EMU, "-f", "S24_3LE", "-r", str(self.fs),
                                "-c", "2", "-d", str(int(out.shape[0] / self.fs + tail + 1)),
                                "-t", "raw", "/tmp/hwf_cap.raw"], stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        with open("/tmp/hwf_play.raw", "wb") as fh:
            v = np.clip(out, -1, 1)
            iv = np.round(v * (1 << 23)).astype("<i4")
            b3 = np.column_stack([iv & 0xFF, (iv >> 8) & 0xFF, (iv >> 16) & 0xFF]).astype(np.uint8)
            fh.write(b3.tobytes())
        subprocess.run(["aplay", "-D", EMU, "-q", "-f", "S24_3LE", "-r", str(self.fs),
                        "-c", "2", "-t", "raw", "/tmp/hwf_play.raw"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec.wait()
        raw = open("/tmp/hwf_cap.raw", "rb").read()
        n = len(raw) // 6
        b = np.frombuffer(raw[: n * 6], dtype=np.uint8).reshape(n, 6)
        x = np.zeros((n, 2))
        for c in (0, 1):
            v = b[:, 3 * c:3 * c + 3].copy().astype(np.int32)
            v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            x[:, c] = v / float(1 << 23)
        return x


def measure_shift(be, shift_l_db):
    t = np.arange(FS * 2) / FS
    tl = 10 ** ((LEVEL + shift_l_db) / 20) * np.sin(2 * np.pi * 1000.0 * t)
    tr = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    rec = be.play_record(np.column_stack([tl, tr]))
    seg = rec[int(0.6 * FS):int(2.3 * FS), :]
    lv = []
    for c in (0, 1):
        rms = float(np.sqrt(np.mean(seg[:, c] ** 2)))
        lv.append(20 * math.log10(max(rms * math.sqrt(2), 1e-12)))
    return lv[0] - lv[1], seg


def main():
    be = EmuHw()
    be.open(FS)
    rig = Rig(be, Calibration(), fs=FS, blocks=2, settle=0.3)
    rig.open()

    # 1) базовая ΔG тракта на трёх частотах (3 повтора)
    print("Базовая ΔG тракта (loopback, 3 повтора):")
    base = {}
    for f in (100.0, 1000.0, 10000.0):
        vals = []
        for rep in range(3):
            res = gainmatch.measure_series(rig, frequencies=(f, f * 1.5), level_dbfs=LEVEL,
                                           repeats=1, purpose="loopback")
            row = next(r for r in res["rows"] if abs(r["frequency_hz"] - f) < f * 0.01)
            vals.append(row["delta_adc_db"])
        base[f] = float(np.mean(vals))
        print(f"  {f:6.0f} Гц: {base[f]:+.3f} дБ (разброс {max(vals)-min(vals):.4f})")
        check(f"HW-BASE {f:.0f} Гц: повторяемость ≤ 0.05 дБ",
              max(vals) - min(vals) <= 0.05,
              f"значения: {['%.3f' % v for v in vals]}")

    # 2) известный сдвиг +0.5 дБ на L
    d05, _ = measure_shift(be, 0.5)
    err05 = d05 - base[1000.0]
    check("HW-SHIFT +0.5 дБ: ОТЛОЖЕНО — помеха сети доминирует над тоном на АЦП "
          "(ручки входа E-MU near minimum)", False,
          f"ΔG={d05:+.3f}; тон на АЦП ≈ −48 дБFS, помеха ≈ −37 дБFS → отношение "
          f"не позволяет ΔG-измерение; нужно поднять входное усиление E-MU")

    # 3) известный сдвиг +1.0 дБ на L
    d10, _ = measure_shift(be, 1.0)
    err10 = d10 - base[1000.0]
    check("HW-SHIFT +1.0 дБ: ОТЛОЖЕНО — та же причина", False,
          f"ΔG={d10:+.3f}")

    # 4) симметрия: −0.5 дБ на L (эквивалент +0.5 на R относительно)
    dm, _ = measure_shift(be, -0.5)
    err_m = (dm - base[1000.0]) + 0.5
    check("HW-SHIFT −0.5 дБ: ОТЛОЖЕНО — та же причина", False,
          f"ΔG={dm:+.3f}")

    # 5) перегрузка: +16 дБ к уровню → вход АЦП клипует
    t = np.arange(FS * 2) / FS
    hot = 10 ** ((LEVEL + 16.0) / 20) * np.sin(2 * np.pi * 1000.0 * t)
    rec_hot = be.play_record(np.column_stack([hot, hot]))
    seg_hot = rec_hot[int(0.6 * FS):int(2.3 * FS), :]
    peak = float(np.max(np.abs(seg_hot)))
    clipped = int(np.sum(np.abs(seg_hot) >= 0.985))
    check("HW-CLIP: перегрузка входа видна (сэмплы у предела)", peak >= 0.9 and clipped > 0,
          f"peak={peak:.3f}, сэмплов ≥0.985: {clipped}")

    # 6) 60-с захват: дрейф + XRUN
    t = np.arange(FS * 60) / FS
    tone = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    t0 = time.time()
    rec60 = be.play_record(np.column_stack([tone, tone]))
    dur = time.time() - t0
    win = FS * 10
    drift = []
    for s in range(int(0.5 * FS), len(rec60) - win, win):
        seg = rec60[s:s + win, :]
        rms = float(np.sqrt(np.mean(seg[:, 0] ** 2)))
        drift.append(20 * math.log10(max(rms * math.sqrt(2), 1e-12)))
    span = max(drift) - min(drift)
    check("HW-LONG: 60 с захвата — дрейф < 0.1 дБ", span < 0.1,
          f"дрейф L = {span:.3f} дБ по 10-с окнам ({len(drift)} окон), "
          f"захват занял {dur:.0f} с")

    npass = sum(1 for _, ok, _ in RES if ok)
    print(f"\nИТОГ аппаратной валидации: {npass}/{len(RES)}")
    import json
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hw-accept-final-results.json"), "w", encoding="utf-8") as fh:
        json.dump({"results": RES, "base_delta": base,
                   "shift_05": d05, "shift_10": d10, "shift_m05": dm}, fh,
                  ensure_ascii=False, indent=1)
    print("JSON: hw-accept-final-results.json")
    sys.exit(0 if npass == len(RES) else 1)


if __name__ == "__main__":
    main()
