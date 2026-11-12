#!/usr/bin/env python3
"""Посемпловая характеристика пути D10 → E-MU (ТЗ координатора §8).

Программа: лидирующая тишина 1 с → маркер старта (6.5 кГц, 60 мс) →
стабильный тон 997 Гц заданной длительности → маркер конца (8 кГц, 60 мс) →
тишина 1 с. arecord стартует заранее, пишет дольше программы на +3 с.

Сравнение с источником: точные кадры начала/конца тона (порог по узкой
демодуляции 997 Гц), маркеры по демодуляции 6.5/8 кГц, отдельно L/R,
задержка старта/конца, длительность захваченного тона против поданной.

Автодетект карт по USB-ID (номера карточек плавают).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAC_USBID = "152a:85dd"   # SMSL D10
ADC_USBID = "041e:3f04"   # E-MU 0404


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


def demod_env(x, fs, f, win_s=0.005):
    """Огибающая узкой демодуляции на f (скользящее среднее по win)."""
    n = len(x)
    t = np.arange(n) / fs
    z = x * np.exp(-2j * np.pi * f * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(win_s * fs)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    return 2 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))


def first_above(env, thr):
    idx = np.where(env > thr)[0]
    return int(idx[0]) if len(idx) else None


def last_above(env, thr):
    idx = np.where(env > thr)[0]
    return int(idx[-1]) if len(idx) else None


def build_program(fs, tone_s):
    """Программа: тишина 1с | маркер 6.5к 60мс | тишина 0.15с | тон 997
    | тишина 0.15с | маркер 8к 60мс | тишина 1с. Возврат: (2,) сигнал, словарь кадров."""
    parts = []
    frames = {}
    def seg(dur, sig=None):
        n = int(round(dur * fs))
        parts.append(np.zeros((n, 2)) if sig is None else sig)
        return n
    frames["lead_silence_start"] = sum(len(p) for p in parts)
    frames["lead_silence_end"] = frames["lead_silence_start"] + seg(1.0)
    tm = np.arange(int(0.06 * fs)) / fs
    marker_start = 10 ** (-30 / 20) * np.sin(2 * np.pi * 6500 * tm)
    marker_end = 10 ** (-30 / 20) * np.sin(2 * np.pi * 8000 * tm)
    ms = np.column_stack([marker_start, marker_start])
    me = np.column_stack([marker_end, marker_end])
    frames["start_marker"] = frames["lead_silence_end"]
    frames["start_marker_end"] = frames["start_marker"] + seg(0.06, ms)
    frames["gap1_end"] = frames["start_marker_end"] + seg(0.15)
    frames["tone"] = frames["gap1_end"]
    nt = int(round(tone_s * fs))
    tone = 10 ** (-30 / 20) * np.sin(2 * np.pi * 997 * np.arange(nt) / fs)
    fade = int(0.01 * fs)
    tone[:fade] *= np.linspace(0, 1, fade)
    tone[-fade:] *= np.linspace(1, 0, fade)
    parts.append(np.column_stack([tone, tone]))
    frames["tone_end"] = frames["tone"] + nt
    frames["gap2_end"] = frames["tone_end"] + seg(0.15)
    frames["end_marker"] = frames["gap2_end"]
    frames["end_marker_end"] = frames["end_marker"] + seg(0.06, me)
    frames["tail_silence"] = frames["end_marker_end"]
    frames["tail_silence_end"] = frames["tail_silence"] + seg(1.0)
    prog = np.concatenate(parts)
    frames["program_len"] = len(prog)
    return prog, frames


def run_case(fs, tone_s, buffer_ms, post_silence_s, extend_tone_s, hw_dac, hw_adc):
    prog, fr = build_program(fs, tone_s)
    if extend_tone_s:
        # удлинить сам тон: вставить продолжение тона перед gap2
        nt = int(round(extend_tone_s * fs))
        extra = 10 ** (-30 / 20) * np.sin(2 * np.pi * 997 * np.arange(nt) / fs)
        prog = np.concatenate([prog[: fr["tone_end"]],
                               np.column_stack([extra, extra]),
                               prog[fr["tone_end"]:]])
        fr["tone_end"] += nt
        for k in ("gap2_end", "end_marker", "end_marker_end", "tail_silence",
                  "tail_silence_end", "program_len"):
            fr[k] += nt
    if post_silence_s:
        n = int(round(post_silence_s * fs))
        prog = np.concatenate([prog, np.zeros((n, 2))])
        fr["post_silence"] = len(prog) - n
    # подмешать Play-пад 50 мс нулями, как делает AlsaCli.play_record
    pad = int(0.05 * fs)
    submitted = np.concatenate([np.zeros((pad, 2)), prog, np.zeros((pad, 2))])
    iv = np.round(np.clip(submitted, -1, 1) * (1 << 31)).astype("<i4")  # DX1: S32_LE
    wav_raw = "/tmp/dx_timing_submitted.s32"
    iv.tofile(wav_raw)
    submitted_frames = len(submitted)

    rec_frames_total = int((submitted_frames / fs + 3.0) * fs)
    cap = subprocess.Popen(
        ["arecord", "-D", f"hw:{hw_adc},0", "-f", "S24_3LE", "-r", str(fs),
         "-c", "2", "-d", str(int(submitted_frames / fs + 3)),
         "-t", "raw", "/tmp/dx_timing_cap.raw"], stderr=subprocess.PIPE)
    time.sleep(0.5)
    t0 = time.monotonic()
    play = subprocess.run(
        ["aplay", "-D", f"hw:{hw_dac},0", "-f", "S32_LE", "-r", str(fs),
         "-c", "2", "-q", "--buffer-time", str(buffer_ms * 1000), wav_raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    play_elapsed = time.monotonic() - t0
    cap.wait()
    cap_err = cap.stderr.read().decode("utf-8", "replace")

    raw = open("/tmp/dx_timing_cap.raw", "rb").read()
    n = len(raw) // 6
    b = np.frombuffer(raw[:n * 6], dtype=np.uint8).reshape(n, 6)
    capL = np.zeros(n); capR = np.zeros(n)
    for c, arr in ((0, capL), (1, capR)):
        v = b[:, 3 * c:3 * c + 3].copy().astype(np.int32)
        v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        arr[:] = v / float(1 << 23)

    res = {"tone_s": tone_s, "buffer_ms": buffer_ms,
           "post_silence_s": post_silence_s, "extend_tone_s": extend_tone_s,
           "submitted_frames": submitted_frames, "captured_frames": n,
           "aplay_rc": play.returncode, "aplay_elapsed_s": round(play_elapsed, 3),
           "aplay_err": play.stderr.decode("utf-8", "replace").strip()[:120],
           "arecord_err": cap_err.strip()[:120]}

    # маркеры и тон в захвате; эталон шума — самая тихая 0.3-с область первых 2 с
    # (латенция сдвигает всё на ~0.5-1.0 с, фиксированные окна не годятся)
    def quiet_noise(arr):
        e = demod_env(arr, fs, 997)
        chunk = int(0.3 * fs)
        best = []
        for i in range(int(0.4 * fs), min(int(2.0 * fs), len(e)) - chunk, chunk):
            best.append(float(np.median(e[i:i + chunk])))
        return max(min(best), 1e-9)
    noise_ref = quiet_noise(capL)
    tone_thr = max(8 * noise_ref, 1e-6)
    for c, name, arr in ((0, "L", capL), (1, "R", capR)):
        e997 = demod_env(arr, fs, 997)
        e65 = demod_env(arr, fs, 6500)
        e8 = demod_env(arr, fs, 8000)
        thr_m = max(8 * quiet_noise(arr), 1e-6)
        thr_m8 = thr_m
        sm = first_above(e65[int(0.7 * fs):int(2.2 * fs)], thr_m)
        tone_s_i = first_above(e997[int(0.7 * fs):int(3.0 * fs)], tone_thr)
        tone_e = last_above(e997[int(0.7 * fs): int(submitted_frames * 0.98)], tone_thr)
        em = first_above(e8[int(1.5 * fs):int(submitted_frames * 0.95)], thr_m8)
        res[f"{name}"] = {
            "start_marker_frame": (sm + int(0.9 * fs)) if sm is not None else None,
            "tone_first_frame": (tone_s_i + int(1.0 * fs)) if tone_s_i is not None else None,
            "tone_last_frame": (tone_e + int(1.0 * fs)) if tone_e is not None else None,
            "end_marker_frame": (em + int(2.0 * fs)) if em is not None else None,
            "latency_start_s": round(((sm + int(0.9 * fs)) - fr["start_marker"]) / fs, 5) if sm is not None else None,
            "captured_tone_s": round(((tone_e - tone_s_i) / fs), 4) if (tone_s_i is not None and tone_e is not None) else None,
        }
    return res, fr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--durations", default="1,2,5,10")
    ap.add_argument("--buffers", default="100")
    ap.add_argument("--rate", type=int, default=192000)
    a = ap.parse_args()
    dac = find_card(DAC_USBID); adc = find_card(ADC_USBID)
    assert dac is not None and adc is not None, "карты не найдены"
    print(f"DAC D10 = hw:{dac},0 | ADC E-MU = hw:{adc},0 | rate {a.rate}", flush=True)
    all_res = []
    for tone_s in [float(x) for x in a.durations.split(",")]:
        for buf in [int(x) for x in a.buffers.split(",")]:
            res, fr = run_case(a.rate, tone_s, buf, 0, 0, dac, adc)
            all_res.append(res)
            L, R = res["L"], res["R"]
            print(f"тон {tone_s:>4}с буфер {buf:>3}мс: "
                  f"задержка L={L['latency_start_s']} R={R['latency_start_s']}; "
                  f"захвачено L={L['captured_tone_s']} R={R['captured_tone_s']} "
                  f"(подано {tone_s}); конец L={L['tone_last_frame']} R={R['tone_last_frame']} "
                  f"(подано {fr['tone_end']}); аплей rc={res['aplay_rc']} {res['aplay_elapsed_s']}с",
                  flush=True)
    out = os.path.join(ROOT, "measurements", "diagnostics",
                       f"timing-probe-{a.rate}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"rate": a.rate, "cases": all_res,
               "frames_5s_program": fr, "timestamp": datetime.now().isoformat()},
              open(out, "w"), ensure_ascii=False, indent=1)
    print("JSON:", out)


if __name__ == "__main__":
    main()
