"""Консольный доступ к измерительным приборам ValveBench."""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from . import calib, measure as m, store, vinyl
from .backend import AlsaCli, SimBackend, SimChannel, list_cards, parse_stream
from .selftest import run_selftest, format_selftest
from .looptest import run_loopback_report
from . import scans, passport
from .measure import Rig


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
    ap.add_argument("--dev", help="ALSA-устройство, например hw:2,0 (номер см. в cards)")
    ap.add_argument("--rate", type=int, help="частота дискретизации")
    ap.add_argument("--blocks", type=int, default=2, help="число когерентных блоков в измерениях тона")
    ap.add_argument("--save", action="store_true", help="сохранить результат в JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cards", help="карты и объявленные режимы (формат/каналы/частоты)")
    sub.add_parser("clock", help="проверка частоты ЦАП/АЦП физическим loopback")
    p = sub.add_parser("loopback", help="серия аппаратных тестов ЦАП→АЦП с повторениями и шумом тишины")
    p.add_argument("--f", type=float, default=997.0, help="частота контрольного тона (997 Гц — простая, как в AES17)")
    p.add_argument("--levels", default="-12,-20,-30,-40", help="уровни ЦАП в dBFS через запятую")
    p.add_argument("--repeats", type=int, default=3, help="повторов на каждом уровне")
    p.add_argument("--silence-seconds", type=float, default=3.0, help="запись тишины до и после серии; 0 отключает")
    p.add_argument("--csv", help="сохранить численные результаты тоновых прогонов в CSV")
    p.add_argument("--report", help="сохранить полный текстовый отчёт в указанный файл")
    p = sub.add_parser("levelscan", help="гармоники против уровня тона: отличить нелинейность от фиксированных линий")
    p.add_argument("--f", type=float, default=997.0)
    p.add_argument("--levels", default="-1,-3,-6,-9,-12,-18,-24,-30", help="уровни ЦАП, dBFS, от громкого к тихому")
    p.add_argument("--report", help="сохранить отчёт в файл")
    p = sub.add_parser("freqscan", help="гармоники на разных частотах теста: периодичность тона или внешний спур")
    p.add_argument("--freqs", default="1000,997,1234", help="частоты тона, Гц, через запятую")
    p.add_argument("--level", type=float, default=-6.0)
    p.add_argument("--report", help="сохранить отчёт в файл")
    p = sub.add_parser("passport", help="паспорт тракта: шум, остаток с тоном, H2/H3, проникновение (для A/B до/после доработки)")
    p.add_argument("--label", default="", help="метка паспорта, напр. before")
    p.add_argument("--rates", default="96000,192000", help="частоты дискретизации через запятую")
    p.add_argument("--quick", action="store_true", help="быстро: одна частота (--rate), −1 dBFS, 3 повтора, ≈45 с")
    p.add_argument("--out", help="файл паспорта (по умолчанию passport-<label>.json в текущем каталоге)")
    p.add_argument("--compare", help="сравнить с ранее снятым паспортом (JSON)")
    p = sub.add_parser("compare", help="сравнить два паспорта: лучше / хуже / в пределах разброса")
    p.add_argument("before"); p.add_argument("after")
    p = sub.add_parser("watch", help="живой монитор остатка L/R при тоне — для работы паяльником")
    p.add_argument("--count", type=int, default=None, help="число замеров (по умолчанию до Ctrl+C)")
    p.add_argument("--baseline", help="паспорт, с которым сравнивать (иначе — первые 3 замера)")
    sub.add_parser("psdcheck", help="сверить плотность шума живого спектра с Welch и интегралом 20–20k")
    p = sub.add_parser("thd"); p.add_argument("--f", type=float, default=997); p.add_argument("--level", type=float, default=-12)
    p = sub.add_parser("thdlevel"); p.add_argument("--f", type=float, default=997)
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
    if a.cmd == "loopback":
        try:
            levels = [float(v.strip()) for v in a.levels.split(",") if v.strip()]
            dev = a.dev or calib.load()["device"]
            if a.sim:
                be = SimBackend(
                    SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=False, noise_dbfs=-105,
                               hum50_dbfs=-105, hum100_dbfs=-112),
                    SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=False, noise_dbfs=-103,
                               hum50_dbfs=-92, hum100_dbfs=-108),
                )
            else:
                if dev == "sim":
                    ap.error("для аппаратной loopback укажи --dev hw:N,0; либо добавь --sim для виртуального теста")
                be = AlsaCli(dev, in_map=calib.load()["in_map"], out_map=calib.load()["out_map"])
            rig = Rig(be, calib.load(), fs=a.rate or calib.load()["fs"], blocks=a.blocks, settle=0.25)
            rig.open()
            report = run_loopback_report(rig, frequency=a.f, levels=levels, repeats=a.repeats,
                                         silence_seconds=a.silence_seconds, csv_path=a.csv)
            print(report)
            if a.report:
                out = Path(a.report).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(report, encoding="utf-8")
                print(f"\nREPORT_SAVED: {out}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"LOOPBACK TEST ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd == "compare":
        text, _v = passport.compare_passports(passport.load(a.before), passport.load(a.after))
        print(text)
        return 0
    if a.cmd in ("passport", "watch", "psdcheck"):
        cfg = calib.load()
        dev = a.dev or cfg["device"]

        def make_rig(fs):
            if a.sim:
                be = SimBackend(SimChannel(gain=0.65, a2=5e-4, a3=3e-3, noise_dbfs=-105, mod_noise_dbc=-80, mod_jitter_db=4.0),
                                SimChannel(gain=0.65, a2=2e-3, a3=1e-3, noise_dbfs=-103))
            else:
                if dev == "sim":
                    ap.error("укажи --dev hw:N,0 или добавь --sim")
                be = AlsaCli(dev, in_map=cfg["in_map"], out_map=cfg["out_map"])
            rig = Rig(be, cfg, fs=int(fs), blocks=max(a.blocks, 4), settle=0.25)
            rig.open()
            return rig
        try:
            if a.cmd == "passport":
                rates = [int(r) for r in a.rates.split(",") if r.strip()]
                if a.quick:
                    rates = [a.rate or cfg["fs"]]
                    kw = dict(levels=(-1.0,), repeats=3, silence_s=4.0)
                else:
                    kw = {}
                label = a.label or time.strftime("%H%M%S")
                p = passport.run_passport(make_rig, rates, label, **kw)
                text = passport.text_passport(p)
                out = a.out or f"passport-{label}.json"
                passport.save(p, out)
                print(text)
                print(f"\nPASSPORT_SAVED: {out}")
                if a.compare:
                    ctext, _v = passport.compare_passports(passport.load(a.compare), p)
                    print("\n" + ctext)
            elif a.cmd == "watch":
                rig = make_rig(a.rate or cfg["fs"])
                base = None
                if a.baseline:
                    bp = passport.load(a.baseline)["rates"].get(str(rig.fs))
                    if bp:
                        tl = bp["tone"]["L"][sorted(bp["tone"]["L"], key=lambda s: -float(s))[0]]
                        tr = bp["tone"]["R"][sorted(bp["tone"]["R"], key=lambda s: -float(s))[0]]
                        base = (tl["resid"], tr["resid"])
                print("Остаток = всё, кроме тона и гармоник. Эталон для сравнения: "
                      + ("паспорт" if base else "первые 3 замера") + ". Ctrl+C — выход.")
                passport.watch_loop(rig, count=a.count, baseline=base, sleep=(lambda s: time.sleep(0.01)) if a.sim else time.sleep)
            else:
                passport.psd_check(make_rig(a.rate or cfg["fs"]))
            return 0
        except KeyboardInterrupt:
            print("\nостановлено")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd in ("levelscan", "freqscan"):
        try:
            dev = a.dev or calib.load()["device"]
            if a.sim:
                be = SimBackend(SimChannel(gain=0.65, a2=5e-4, a3=3e-3, noise_dbfs=-105, quant_bits=19),
                                SimChannel(gain=0.65, a2=2e-3, a3=1e-3, noise_dbfs=-103, quant_bits=19))
            else:
                if dev == "sim":
                    ap.error("укажи --dev hw:N,0 или добавь --sim")
                be = AlsaCli(dev, in_map=calib.load()["in_map"], out_map=calib.load()["out_map"])
            rig = Rig(be, calib.load(), fs=a.rate or calib.load()["fs"], blocks=a.blocks, settle=0.25)
            rig.open()
            if a.cmd == "levelscan":
                res = scans.run_level_scan(rig, a.f, [float(v) for v in a.levels.split(",") if v.strip()])
                report = scans.text_level_scan(res)
            else:
                res = scans.run_freq_scan(rig, [float(v) for v in a.freqs.split(",") if v.strip()], a.level)
                report = scans.text_freq_scan(res)
            print(report)
            if a.report:
                out = Path(a.report).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(report, encoding="utf-8")
                print(f"\nREPORT_SAVED: {out}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"SCAN ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
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
