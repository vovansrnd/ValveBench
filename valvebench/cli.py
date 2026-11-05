"""Консольный доступ к измерительным приборам ValveBench."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from . import calib, measure as m, store, vinyl
from .backend import AlsaCli, SimBackend, SimChannel, list_cards, parse_stream
from .selftest import run_selftest, format_selftest


def make_rig(a):
    cal = calib.load()
    if a.sim:
        be = SimBackend(
            SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=True, noise_dbfs=-105,
                       hum50_dbfs=-105, hum100_dbfs=-112),
            SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=True, noise_dbfs=-103,
                       hum50_dbfs=-92, hum100_dbfs=-108),
        )
    else:
        be = AlsaCli(a.dev or cal["device"], in_map=cal["in_map"], out_map=cal["out_map"])
    rig = m.Rig(be, cal, fs=a.rate or cal["fs"], blocks=a.blocks)
    rig.open()
    for note in be.notes:
        print("! " + note, file=sys.stderr)
    return rig


def main(argv=None):
    ap = argparse.ArgumentParser(prog="valvebench cli", description=__doc__)
    ap.add_argument("--sim", action="store_true", help="виртуальная карта (без железа)")
    ap.add_argument("--dev", help="ALSA-устройство, например hw:1,0")
    ap.add_argument("--rate", type=int, help="частота дискретизации")
    ap.add_argument("--blocks", type=int, default=2, help="число когерентных блоков в измерениях тона")
    ap.add_argument("--save", action="store_true", help="сохранить результат в JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cards", help="карты и объявленные режимы (формат/каналы/частоты)")
    sub.add_parser("clock", help="проверка частоты ЦАП/АЦП физическим loopback")
    p = sub.add_parser("thd"); p.add_argument("--f", type=float, default=1000); p.add_argument("--level", type=float, default=-12)
    p = sub.add_parser("thdlevel"); p.add_argument("--f", type=float, default=1000)
    p.add_argument("--levels", default="-6,-12,-18,-24,-36,-48")
    p = sub.add_parser("imd"); p.add_argument("--preset", default="CCIF 19+20 кГц", choices=list(m.IMD_PRESETS))
    p.add_argument("--level", type=float, default=-12)
    p = sub.add_parser("noise"); p.add_argument("--seconds", type=float, default=8)
    p = sub.add_parser("noise-response"); p.add_argument("--kind", choices=("white", "pink", "riaa"), default="pink")
    p.add_argument("--seconds", type=float, default=8); p.add_argument("--level", type=float, default=-36)
    p.add_argument("--lo", type=float, default=20); p.add_argument("--hi", type=float, default=20000)
    p.add_argument("--iec", action="store_true"); p.add_argument("--csv")
    sub.add_parser("xtalk")
    p = sub.add_parser("sweep"); p.add_argument("--mode", choices=("riaa", "flat"), default="riaa")
    p.add_argument("--iec", action="store_true"); p.add_argument("--level", type=float, default=-24)
    p.add_argument("--dur", type=float, default=8); p.add_argument("--csv")
    p = sub.add_parser("record"); p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--wav", required=True, help="куда сохранить стерео WAV")
    p = sub.add_parser("vinyl"); p.add_argument("--file", required=True)
    p.add_argument("--test", choices=("3150", "phase", "sum", "skating", "resonance", "xtalk-L", "xtalk-R", "response"), required=True)
    p.add_argument("--start", type=float, default=0.0, help="начало анализируемого участка, секунды")
    p.add_argument("--duration", type=float, help="длительность участка, секунды")
    p.add_argument("--channel", choices=("L", "R", "mono"), default="mono")

    a = ap.parse_args(argv)
    if a.cmd == "cards":
        cards = list_cards()
        if not cards:
            print("ALSA cards: none")
        for no, name in cards:
            print(f"card {no}: {name}")
            for direction, alts in parse_stream(no).items():
                for alt in alts:
                    print(f"  {direction:8s} format={alt.fmt} channels={alt.channels} rates={','.join(map(str, alt.rates))}")
        return 0
    if a.cmd == "vinyl":
        fs, x = vinyl.read_wav(a.file)
        start = max(0, int(a.start * fs))
        end = len(x) if a.duration is None else min(len(x), start + int(a.duration * fs))
        x = x[start:end]
        data = x if a.channel == "mono" or a.test == "response" else x[:, 0 if a.channel == "L" else 1]
        if a.test == "response":
            res = vinyl.analyze_frequency_response(x, fs)
        elif a.test == "3150":
            res = vinyl.analyze_3150_stability(data, fs)
        elif a.test == "phase":
            res = vinyl.analyze_phase(x, fs)
        elif a.test == "sum":
            res = vinyl.analyze_vta_sum(data, fs)
        elif a.test == "skating":
            res = vinyl.analyze_skating_300(x, fs)
        elif a.test == "xtalk-L":
            res = vinyl.analyze_crosstalk(x, fs, active="L")
        elif a.test == "xtalk-R":
            res = vinyl.analyze_crosstalk(x, fs, active="R")
        else:
            res = vinyl.analyze_arm_resonance(data, fs)
        print(vinyl.text_result(res))
        return 0
    if a.cmd == "selftest":
        raise SystemExit("selftest is available as top-level command: ./valvebench selftest")
    if a.cmd == "record":
        cal = calib.load()
        be = AlsaCli(a.dev or cal["device"], in_map=cal["in_map"], out_map=cal["out_map"])
        be.open(a.rate or cal["fs"])
        x = be.record(a.seconds)
        vinyl.write_wav(a.wav, be.fs, x)
        print(f"Записано {len(x)/be.fs:.2f} с, {be.fs} Гц, 2 канала: {a.wav}")
        for note in be.notes:
            print("! " + note)
        return 0

    rig = make_rig(a)
    res = None
    if a.cmd == "clock":
        res = m.clock_check(rig); text = m.text_clock(res)
    elif a.cmd == "thd":
        res = m.thd_point(rig, a.f, a.level); text = m.text_thd(res)
    elif a.cmd == "thdlevel":
        lv = [float(x.strip()) for x in a.levels.split(",") if x.strip()]
        res = m.thd_vs_level(rig, a.f, lv); text = m.text_thd_level(res)
    elif a.cmd == "imd":
        f1, f2, mode = m.IMD_PRESETS[a.preset]
        res = m.imd(rig, f1, f2, mode, a.level); text = m.text_imd(res)
    elif a.cmd == "noise":
        res = m.noise(rig, a.seconds); text = m.text_noise(res)
    elif a.cmd == "noise-response":
        res = m.noise_response(rig, a.seconds, a.kind, a.level, a.lo, a.hi, a.iec)
        text = m.text_noise_response(res)
        if a.csv:
            with open(a.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(["freq_hz", "L_RIAA_dev_db", "R_RIAA_dev_db", "L_coherence", "R_coherence"])
                for i, fq in enumerate(res["ch"]["L"]["freq"]):
                    w.writerow([fq, res["ch"]["L"]["riaa_dev_db"][i], res["ch"]["R"]["riaa_dev_db"][i],
                                res["ch"]["L"]["coherence"][i], res["ch"]["R"]["coherence"][i]])
    elif a.cmd == "xtalk":
        res = m.crosstalk(rig); text = m.text_crosstalk(res)
    elif a.cmd == "sweep":
        res = m.sweep(rig, mode=a.mode, iec=a.iec, level_1k_dbfs=a.level, dur=a.dur)
        text = m.text_sweep(res)
        if a.csv:
            store.sweep_csv(res, a.csv)
    else:
        ap.error("неизвестная команда")
    print(text)
    if a.save and res is not None:
        print("сохранено:", store.save_json(res))
    return 0
