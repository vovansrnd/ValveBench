#!/usr/bin/env python3
"""Аппаратная приёмка обеих карт: E-MU 0404 USB + ASUS STRIX RAID DLX."""
import math
import os
import subprocess
import sys
import time

import numpy as np

FS = 48000
LEVEL = -6.0

CARDS = {}


def find_cards():
    for c in os.listdir("/proc/asound"):
        if not c.startswith("card"):
            continue
        try:
            usbid = open(f"/proc/asound/{c}/usbid").read().strip()
        except OSError:
            continue
        if usbid == "041e:3f04":
            CARDS["E-MU"] = f"hw:{c[4:]},0"
        elif usbid == "0b05:180c":
            CARDS["ASUS"] = f"hw:{c[4:]},0"


def float_to_s24le(x):
    """float (N, ch) → S24_3LE байты: для каждого кадра 3 байта ch0, затем 3 байта ch1."""
    x = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    n, ch = x.shape
    iv = np.round(x * float(1 << 23)).astype(np.int64)
    iv = np.clip(iv, -(1 << 23), (1 << 23) - 1)
    b3 = np.zeros((n, ch, 3), dtype=np.uint8)
    for byte in range(3):
        b3[:, :, byte] = ((iv >> (8 * byte)) & 0xFF).astype(np.uint8)
    return b3.tobytes()


def parse_s24le(raw):
    n = len(raw) // 6
    b = np.frombuffer(raw[:n * 6], dtype=np.uint8).reshape(n, 6)
    x = np.zeros((n, 2))
    for c in (0, 1):
        v = b[:, 3 * c:3 * c + 3].copy().astype(np.int32)
        v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x[:, c] = v / float(1 << 23)
    return x


def capture_play(hw, play_frames, rec_seconds):
    with open("/tmp/hw_play.raw", "wb") as fh:
        fh.write(float_to_s24le(play_frames))
    cap = "/tmp/hw_cap.raw"
    rec = subprocess.Popen(["arecord", "-D", hw, "-f", "S24_3LE", "-r", str(FS),
                            "-c", "2", "-d", str(int(rec_seconds)), "-t", "raw", cap],
                           stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    subprocess.run(["aplay", "-D", hw, "-f", "S24_3LE", "-r", str(FS),
                    "-c", "2", "-t", "raw", "/tmp/hw_play.raw"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec.wait()
    return parse_s24le(open(cap, "rb").read())


def measure_tone_level(x, f, fs):
    demod = np.exp(-2j * np.pi * f * np.arange(len(x)) / fs)
    z = np.mean(x * demod) * 2
    return 20 * math.log10(max(abs(z), 1e-12))


def run_card(name, hw):
    t0 = time.time()
    t = np.arange(FS * 2) / FS
    ok = lambda b: "PASS" if b else "FAIL"
    print(f"\n===== {name} ({hw}) =====")

    # шумовой пол
    nf_rec = capture_play(hw, np.zeros((FS * 2, 2)), 3)
    nf_seg = nf_rec[FS // 2:FS + FS // 2, :]
    nf = [20 * math.log10(max(float(np.sqrt(np.mean(nf_seg[:, c] ** 2))) * math.sqrt(2), 1e-12))
          for c in (0, 1)]
    print(f"  шумовой пол: L={nf[0]:.1f}, R={nf[1]:.1f} дБFS")

    # тон 1 кГц
    tone = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    tone_rec = capture_play(hw, np.column_stack([tone, tone]), 4)
    seg = tone_rec[int(0.6 * FS):int(2.3 * FS), :]
    lv = [measure_tone_level(seg[:, c], 1000.0, FS) for c in (0, 1)]
    dg = lv[0] - lv[1]
    peak = float(np.max(np.abs(seg)))
    print(f"  1 кГц: L={lv[0]:.2f}, R={lv[1]:.2f} дБFS, ΔG={dg:+.2f}, peak={peak:.3f}")
    tone_ok = min(lv) > nf[0] + 10

    # ΔG на 100 и 10 кГц
    dg_by_freq = {}
    for f in (100.0, 10000.0):
        tone_f = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * f * t)
        rec_f = capture_play(hw, np.column_stack([tone_f, tone_f]), 4)
        seg_f = rec_f[int(0.6 * FS):int(2.3 * FS), :]
        lv_f = [measure_tone_level(seg_f[:, c], f, FS) for c in (0, 1)]
        dg_by_freq[f] = lv_f[0] - lv_f[1]
        print(f"  {f:.0f} Гц: L={lv_f[0]:.2f}, R={lv_f[1]:.2f}, ΔG={dg_by_freq[f]:+.2f}")

    # известный сдвиг +0.5 дБ
    tl = 10 ** ((LEVEL + 0.5) / 20) * np.sin(2 * np.pi * 1000.0 * t)
    tr = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    rec_s = capture_play(hw, np.column_stack([tl, tr]), 4)
    seg_s = rec_s[int(0.6 * FS):int(2.3 * FS), :]
    lv_s = [measure_tone_level(seg_s[:, c], 1000.0, FS) for c in (0, 1)]
    dg_shift = lv_s[0] - lv_s[1]
    err = abs(dg_shift - dg)
    print(f"  сдвиг +0.5: ΔG={dg_shift:+.2f}, ошибка={err:.2f} дБ")
    shift_ok = err < 0.1

    # повторяемость
    dg_reps = []
    for i in range(3):
        rec_r = capture_play(hw, np.column_stack([tone, tone]), 4)
        seg_r = rec_r[int(0.6 * FS):int(2.3 * FS), :]
        lv_r = [measure_tone_level(seg_r[:, c], 1000.0, FS) for c in (0, 1)]
        dg_reps.append(lv_r[0] - lv_r[1])
    rep_span = max(dg_reps) - min(dg_reps)
    print(f"  повторяемость ΔG: {['%.3f' % v for v in dg_reps]}, разброс {rep_span:.3f} дБ")

    # итог
    status = "OK" if (tone_ok and shift_ok and rep_span < 0.1) else "ОГРАНИЧЕНО"
    print(f"  СТАТУС: {status}")
    return {"name": name, "hw": hw, "nf": nf, "levels_1k": lv, "dg_1k": dg,
            "dg_100": dg_by_freq.get(100.0), "dg_10k": dg_by_freq.get(10000.0),
            "dg_shift_05": dg_shift, "shift_err": err,
            "dg_reps": dg_reps, "rep_span": rep_span, "status": status}


find_cards()
print("Карты:", {k: v for k, v in CARDS.items()})
if len(CARDS) < 2:
    print("ОЖИДАЮТСЯ ОБЕ КАРТЫ"); sys.exit(1)

results = {}
for name, hw in CARDS.items():
    results[name] = run_card(name, hw)

print("\n=== СВОДКА ===")
for name, r in results.items():
    print(f"{name}: ΔG@1к={r['dg_1k']:+.2f}, ΔG@100={r['dg_100']:+.2f}, "
          f"ΔG@10к={r['dg_10k']:+.2f}, сдвиг_ошибка={r['shift_err']:.2f}, "
          f"повторяемость={r['rep_span']:.3f}, статус={r['status']}")

import json
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "hw-accept-both-cards.json"), "w", encoding="utf-8") as fh:
    json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
print("JSON: hw-accept-both-cards.json")
