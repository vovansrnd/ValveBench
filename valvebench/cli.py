"""Консольный доступ к измерительным приборам ValveBench."""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from . import calib, measure as m, store, vinyl, dutcompare
from .backend import (AlsaCli, SimBackend, SimChannel, list_cards, parse_stream,
                      list_pcm_devices, list_duplex_devices, supported_duplex_rates,
                      supported_pcm_rates, common_pcm_rates)
from .selftest import run_selftest, format_selftest
from .looptest import run_loopback_report
from . import scans, passport, steady, glitch_test
from .measure import Rig


def selected_audio_pair(args, cal):
    """Resolve playback/capture endpoints; --dev remains a legacy both-sides override."""
    if getattr(args, "dev", None):
        dac_hw = adc_hw = str(args.dev)
    else:
        legacy = str(cal.get("device", "sim"))
        dac_hw = str(getattr(args, "dac", None) or cal.get("dac_device") or legacy)
        adc_hw = str(getattr(args, "adc", None) or cal.get("adc_device") or legacy)
    if (dac_hw == "sim") != (adc_hw == "sim"):
        raise ValueError("Симулятор должен быть выбран одновременно для ЦАП и АЦП")
    return dac_hw, adc_hw


def make_alsa_backend(args, cal):
    dac_hw, adc_hw = selected_audio_pair(args, cal)
    return AlsaCli(hw_out=dac_hw, hw_in=adc_hw,
                   in_map=cal["in_map"], out_map=cal["out_map"])


def validate_pair_rate(dac_hw, adc_hw, fs):
    """Reject a requested exact-measurement Fs outside the advertised pair intersection."""
    if dac_hw == adc_hw == "sim":
        return
    rates = common_pcm_rates(dac_hw, adc_hw)
    if not rates:
        raise ValueError(f"У ЦАП {dac_hw} и АЦП {adc_hw} нет общей объявленной частоты дискретизации")
    if int(fs) not in rates:
        raise ValueError(f"{int(fs)} Гц не входит в общие режимы ЦАП/АЦП; доступно: "
                         + ", ".join(str(v) for v in rates))


def split_clock_preflight(rig, dac_hw, adc_hw, level_dbfs=-72.0):
    """Check independent physical clocks at an explicitly selected safe level."""
    if str(dac_hw) == str(adc_hw):
        return None
    result = m.clock_check(rig, level_dbfs=level_dbfs)
    if not result.get("ok", False):
        raise m.MeasureError(
            "Не удалось подтвердить пригодность раздельной пары ЦАП/АЦП: "
            + m.text_clock(result)
        )
    rig.clock_result = result
    return result


def require_usable_clock_for_resampling(command, clock_result):
    """Require a valid measured ratio before applying split-clock resampling.

    FFT-window coherence is intentionally not a rejection criterion here:
    that offset is precisely what the capture resampler corrects.
    """
    if command not in ("sweep", "noise-response") or clock_result is None:
        return
    if not (clock_result.get("ok") and clock_result.get("clock_ok")
            and clock_result.get("signal_ok")):
        raise m.MeasureError(
            f"Команда {command}: нет достоверной оценки отношения частот ЦАП/АЦП; "
            "коррекция временной базы не выполнена."
        )


def make_rig(a):
    cal = calib.load()
    if a.sim:
        dac_hw = adc_hw = "sim"
        be = SimBackend(
            SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=True, noise_dbfs=-105,
                       hum50_dbfs=-105, hum100_dbfs=-112),
            SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=True, noise_dbfs=-103,
                       hum50_dbfs=-92, hum100_dbfs=-108),
        )
    else:
        dac_hw, adc_hw = selected_audio_pair(a, cal)
        be = make_alsa_backend(a, cal)
    fs = int(a.rate or cal["fs"])
    validate_pair_rate(dac_hw, adc_hw, fs)
    rig = m.Rig(be, cal, fs=fs, blocks=a.blocks)
    rig.open()
    for note in be.notes:
        print("! " + note, file=sys.stderr)
    # A pure ADC noise capture does not depend on DAC/ADC clock coherence.
    # All generated-tone, sweep and H1 tests on split devices must preflight.
    if not a.sim and a.cmd not in ("clock", "noise"):
        result = split_clock_preflight(rig, dac_hw, adc_hw,
                                        level_dbfs=a.clock_level)
        if result:
            require_usable_clock_for_resampling(a.cmd, result)
            print(m.text_clock(result), file=sys.stderr)
    return rig


def main(argv=None):
    ap = argparse.ArgumentParser(prog="valvebench cli", description=__doc__)
    ap.add_argument("--sim", action="store_true", help="виртуальная карта (без железа)")
    ap.add_argument("--dev", help="старый общий ALSA endpoint для ЦАП и АЦП, например hw:2,0")
    ap.add_argument("--dac", help="ALSA PCM для воспроизведения/ЦАП, например hw:2,0")
    ap.add_argument("--adc", help="ALSA PCM для записи/АЦП, например hw:3,0")
    ap.add_argument("--rate", type=int, help="частота дискретизации")
    ap.add_argument("--blocks", type=int, default=2, help="число когерентных блоков в измерениях тона")
    ap.add_argument("--clock-level", type=float, default=-72.0,
                    help="уровень контрольного тона раздельных ЦАП/АЦП, dBFS; "
                         "повышайте только при заведомо безопасном физическом тракте")
    ap.add_argument("--save", action="store_true", help="сохранить результат в JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cards", help="карты и объявленные режимы (формат/каналы/частоты)")
    sub.add_parser("devices", help="отдельные PCM endpoints ЦАП/АЦП и общий полный дуплекс")
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
    p = sub.add_parser("passport", help="паспорт измерительного тракта: тишина, тон, гармоники, остаток, проникновение")
    p.add_argument("--label", default="", help="метка серии: before / after / имя карты")
    p.add_argument("--rates", default="diagnostic", help="частоты через запятую, diagnostic (96/176.4/192 только поддерживаемые) или supported")
    p.add_argument("--quick", action="store_true", help="одна частота --rate, уровень -1 dBFS, 3 повтора")
    p.add_argument("--out", help="путь JSON-файла паспорта")
    p.add_argument("--compare", help="путь к паспорту для A/B-сравнения")
    p.add_argument("--allow-device-mismatch", action="store_true", help="разрешить исследовательское сравнение разных карт/конфигураций")
    p = sub.add_parser("compare", help="сравнить два паспорта A/B")
    p.add_argument("before"); p.add_argument("after")
    p.add_argument("--allow-device-mismatch", action="store_true", help="разрешить сравнение разных карт/конфигураций с предупреждением")
    p = sub.add_parser("watch", help="непрерывный монитор негармонического остатка L/R при тоне")
    p.add_argument("--count", type=int, default=None, help="число замеров; по умолчанию до Ctrl+C")
    p.add_argument("--baseline", help="JSON-паспорт для сравнения")
    sub.add_parser("psdcheck", help="сверить живой PSD со Welch и интегралом")
    p = sub.add_parser("steady", help="длительный matched-level тест с отдельными ЦАП и АЦП (можно разных карт)")
    p.add_argument("--play-dev", required=True, help="ALSA PCM ЦАП, например hw:3,0")
    p.add_argument("--rec-dev", required=True, help="ALSA PCM АЦП, например hw:2,0")
    p.add_argument("--tone", type=float, default=997.0, help="частота тестового тона, Гц")
    p.add_argument("--tone-level", type=float, default=-18.0, help="цифровой уровень ЦАП, dBFS")
    p.add_argument("--target", type=float, default=-23.4, help="целевой уровень на АЦП, dBFS")
    p.add_argument("--tolerance", type=float, default=1.0, help="допуск согласования уровня, дБ")
    p.add_argument("--duration", type=float, default=40.0, help="длительность L-only и R-only, секунд")
    p.add_argument("--window", type=float, default=0.1, help="окно анализа, секунд")
    p.add_argument("--hop", type=float, default=0.1, help="шаг анализа, секунд")
    p.add_argument("--harmonics", type=int, default=20, help="сколько гармоник вычитать LS-проекцией")
    p.add_argument("--idle-seconds", type=float, default=15.0, help="длительность тишины до и после теста")
    p.add_argument("--gap-seconds", type=float, default=8.0, help="пауза между L-only и R-only")
    p.add_argument("--play-format", default="auto", help="auto, S24_3LE, S24_LE, S32_LE, S16_LE")
    p.add_argument("--rec-format", default="auto", help="auto, S24_3LE, S24_LE, S32_LE, S16_LE")
    p.add_argument("--outdir", default="emu_measurements", help="каталог для summary/CSV/WAV")
    p = sub.add_parser("glitch", help="локализация сбойных отсчётов и остановок потока: один тон или тишина (опыты A–E)")
    p.add_argument("--play-dev", required=True, help="ALSA PCM ЦАП, например hw:2,0")
    p.add_argument("--rec-dev", required=True, help="ALSA PCM АЦП, например hw:2,0")
    p.add_argument("--channel", choices=("L", "R", "both", "none"), default="L", help="куда подать тон; none — тишина")
    p.add_argument("--tone", type=float, default=997.0, help="частота тона, Гц")
    p.add_argument("--tone-level", type=float, default=-18.0, help="цифровой уровень ЦАП, dBFS")
    p.add_argument("--seconds", type=float, default=60.0, help="длительность устойчивого участка, секунд (10–600)")
    p.add_argument("--idle-seconds", type=float, default=6.0, help="тишина до и после, секунд")
    p.add_argument("--keep-wav", action="store_true", help="сохранить запись как float32 WAV")
    p.add_argument("--outdir", default="emu_measurements", help="каталог для summary/CSV/JSON")
    p = sub.add_parser("steady-compare", help="свести несколько steady.json в одну сравнительную таблицу")
    p.add_argument("runs", nargs="+", help="пути к steady.json или каталогам измерений")
    p.add_argument("--csv", help="дополнительно сохранить таблицу в CSV")
    p = sub.add_parser("dut-compare", help="сравнить сохранённые пакеты нескольких DUT при одинаковых условиях")
    p.add_argument("sessions", nargs="+", help="пути к каталогам сессий или session.json")
    p.add_argument("--out-dir", help="каталог для comparison.json и CSV; по умолчанию measurements/comparisons/")
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
                grouped = {}
                for alt in alts:
                    grouped.setdefault((alt.fmt, alt.channels), set()).update(alt.rates)
                for (fmt, channels), rates in sorted(grouped.items()):
                    print(f"  {direction:8s} format={fmt} channels={channels} rates={','.join(map(str, sorted(rates)))}")
        devices = list_duplex_devices()
        print("\nДуплексные PCM endpoints (можно использовать как --dev / --play-dev / --rec-dev):")
        if not devices:
            print("  не найдены; проверь arecord -l и aplay -l")
        for hw, label in devices:
            print("  " + label)
        return 0
    if a.cmd == "devices":
        pcm = list_pcm_devices()
        for direction in ("Playback", "Capture"):
            title = "ЦАП / воспроизведение" if direction == "Playback" else "АЦП / запись"
            print(f"\n{title}:")
            if not pcm[direction]:
                print("  устройств не найдено")
            for hw, label in pcm[direction]:
                rates = supported_pcm_rates(hw, direction)
                rate_text = ",".join(map(str, rates)) if rates else "stream0 не сообщает; проверять открытием"
                print(f"  {label} · advertised {direction.lower()} rates={rate_text}")
        duplex = list_duplex_devices()
        print("\nПолный дуплекс на одном PCM:")
        if not duplex:
            print("  нет; для измерения между разными картами используй --play-dev и --rec-dev")
        for hw, label in duplex:
            print("  " + label)
        print("\nВНИМАНИЕ: номера card:N меняются после переподключения USB; перед опытом снова запусти cards/devices.")
        return 0
    if a.cmd == "compare":
        try:
            text, _stats = passport.compare_passports(passport.load(a.before), passport.load(a.after),
                                                       allow_mismatch=a.allow_device_mismatch)
            print(text)
            return 0
        except Exception as exc:
            print(f"COMPARE ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd == "steady":
        try:
            steady.run_steady_test(
                play_dev=a.play_dev, rec_dev=a.rec_dev, fs=a.rate or 192000,
                tone_hz=a.tone, tone_level_dbfs=a.tone_level, target_dbfs=a.target,
                tolerance_db=a.tolerance, duration_s=a.duration, window_s=a.window,
                hop_s=a.hop, max_harmonics=a.harmonics, idle_s=a.idle_seconds,
                gap_s=a.gap_seconds, play_format=a.play_format,
                rec_format=a.rec_format, out_root=a.outdir)
            return 0
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            return 130
        except Exception as exc:
            print(f"STEADY TEST ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd == "glitch":
        try:
            glitch_test.run_glitch_test(
                play_dev=a.play_dev, rec_dev=a.rec_dev, fs=a.rate or 192000, channel=a.channel,
                tone_level_dbfs=a.tone_level, seconds=a.seconds, tone_hz=a.tone, idle_s=a.idle_seconds,
                out_root=a.outdir, keep_wav=a.keep_wav)
            return 0
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            return 130
        except Exception as exc:
            print(f"GLITCH TEST ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd == "dut-compare":
        try:
            comparison = dutcompare.compare_sessions(a.sessions)
            print(dutcompare.text_comparison(comparison))
            output = dutcompare.save_comparison(comparison, a.out_dir)
            print(f"\\nСРАВНЕНИЕ СОХРАНЕНО: {output}")
            return 0
        except Exception as exc:
            print(f"DUT COMPARE ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd == "steady-compare":
        try:
            text, rows = steady.compare_steady_runs(a.runs)
            print(text)
            if a.csv:
                if not rows:
                    raise ValueError("Нет измерений для записи CSV")
                import csv as _csv
                out = Path(a.csv)
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.open("w", newline="", encoding="utf-8") as fh:
                    writer = _csv.DictWriter(fh, fieldnames=list(rows[0]))
                    writer.writeheader(); writer.writerows(rows)
                print(f"CSV сохранён: {out}")
            return 0
        except Exception as exc:
            print(f"STEADY COMPARE ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd in ("passport", "watch", "psdcheck"):
        cfg = calib.load()
        dac_hw, adc_hw = ("sim", "sim") if a.sim else selected_audio_pair(a, cfg)
        dev = "sim" if dac_hw == adc_hw == "sim" else f"{dac_hw}|{adc_hw}"

        clock_checks = []
        def make_rig_pair(fs):
            validate_pair_rate(dac_hw, adc_hw, int(fs))
            if a.sim:
                be = SimBackend(
                    SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=False, noise_dbfs=-105,
                               mod_noise_dbc=-80, mod_jitter_db=4.0),
                    SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=False, noise_dbfs=-103))
            else:
                if dev == "sim":
                    raise ValueError("Укажи --dac/--adc или добавь --sim")
                be = AlsaCli(hw_out=dac_hw, hw_in=adc_hw,
                             in_map=cfg["in_map"], out_map=cfg["out_map"])
            rig = Rig(be, cfg, fs=int(fs), blocks=max(a.blocks, 4), settle=0.25)
            rig.open()
            if not a.sim and a.cmd in ("passport", "watch"):
                ck = split_clock_preflight(rig, dac_hw, adc_hw, level_dbfs=a.clock_level)
                if ck:
                    clock_checks.append({"fs": int(fs), **ck})
            return rig
        try:
            if a.cmd == "passport":
                if a.quick:
                    rates = [a.rate or cfg["fs"]]
                    levels, repeats, silence_s = (-1.0,), 3, 4.0
                else:
                    rates_arg = a.rates.strip().lower()
                    known = (44100, 48000, 88200, 96000, 176400, 192000)
                    if rates_arg == "diagnostic":
                        if dev == "sim":
                            rates = [96000, 192000]
                        else:
                            advertised = set(common_pcm_rates(dac_hw, adc_hw, known))
                            rates = [r for r in (96000, 176400, 192000) if r in advertised]
                    elif rates_arg == "supported":
                        rates = [r for r in common_pcm_rates(dac_hw, adc_hw, known) if r in known]
                    else:
                        rates = [int(v.strip()) for v in a.rates.split(",") if v.strip()]
                        advertised = set(common_pcm_rates(dac_hw, adc_hw, known)) if dev != "sim" else set(known)
                        unsupported = [r for r in rates if r not in advertised]
                        if unsupported:
                            raise ValueError(f"Пара ЦАП/АЦП {dev} не объявляет общие частоты {unsupported}; доступно {sorted(advertised)}")
                    if not rates:
                        raise ValueError(f"Не удалось определить подходящие частоты для {dev}; задай --rates явно после ./run.sh cli cards")
                    levels, repeats, silence_s = (-1.0, -12.0), 3, 8.0
                label = a.label or time.strftime("%Y%m%d-%H%M%S")
                metadata = {"in_map": list(cfg["in_map"]), "out_map": list(cfg["out_map"]),
                            "blocks": max(a.blocks, 4), "device": dev}
                p = passport.run_passport(make_rig_pair, rates, label, levels=levels, repeats=repeats,
                                          silence_s=silence_s, metadata=metadata)
                if clock_checks:
                    p.setdefault("metadata", {})["split_clock_checks"] = clock_checks
                report = passport.text_passport(p)
                out = Path(a.out or f"passport-{label}.json")
                out.parent.mkdir(parents=True, exist_ok=True)
                passport.save(p, out)
                print(report)
                print(f"\nPASSPORT_SAVED: {out}")
                if a.compare:
                    ctext, _stats = passport.compare_passports(passport.load(a.compare), p,
                                                               allow_mismatch=a.allow_device_mismatch)
                    print("\n" + ctext)
            elif a.cmd == "watch":
                rig = make_rig_pair(a.rate or cfg["fs"])
                base = None
                if a.baseline:
                    bp = passport.load(a.baseline)["rates"].get(str(rig.fs))
                    if bp:
                        tl = bp["tone"]["L"][sorted(bp["tone"]["L"], key=lambda q: -float(q))[0]]
                        tr = bp["tone"]["R"][sorted(bp["tone"]["R"], key=lambda q: -float(q))[0]]
                        base = (tl["resid"], tr["resid"])
                print("Негармонический остаток = всё, кроме тона и обнаруженных гармоник. Ctrl+C — выход.")
                passport.watch_loop(rig, count=a.count, baseline=base)
            else:
                psd_result = passport.psd_check(make_rig_pair(a.rate or cfg["fs"]))
                print("PSD_CHECK_COMPLETE")
            return 0
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            return 130
        except Exception as exc:
            print(f"PASSPORT ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if a.cmd == "loopback":
        try:
            levels = [float(v.strip()) for v in a.levels.split(",") if v.strip()]
            cfg = calib.load()
            dac_hw, adc_hw = ("sim", "sim") if a.sim else selected_audio_pair(a, cfg)
            dev = "sim" if dac_hw == adc_hw == "sim" else f"{dac_hw}|{adc_hw}"
            if a.sim:
                be = SimBackend(
                    SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=False, noise_dbfs=-105,
                               hum50_dbfs=-105, hum100_dbfs=-112),
                    SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=False, noise_dbfs=-103,
                               hum50_dbfs=-92, hum100_dbfs=-108),
                )
            else:
                if dev == "sim":
                    ap.error("для аппаратной loopback укажи --dac/--adc; либо добавь --sim")
                be = AlsaCli(hw_out=dac_hw, hw_in=adc_hw,
                             in_map=cfg["in_map"], out_map=cfg["out_map"])
            validate_pair_rate(dac_hw, adc_hw, a.rate or cfg["fs"])
            rig = Rig(be, cfg, fs=a.rate or cfg["fs"], blocks=a.blocks, settle=0.25)
            rig.open()
            if not a.sim:
                split_clock_preflight(rig, dac_hw, adc_hw, level_dbfs=a.clock_level)
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
    if a.cmd in ("levelscan", "freqscan"):
        try:
            cfg = calib.load()
            dac_hw, adc_hw = ("sim", "sim") if a.sim else selected_audio_pair(a, cfg)
            dev = "sim" if dac_hw == adc_hw == "sim" else f"{dac_hw}|{adc_hw}"
            if a.sim:
                be = SimBackend(SimChannel(gain=0.65, a2=5e-4, a3=3e-3, noise_dbfs=-105, quant_bits=19),
                                SimChannel(gain=0.65, a2=2e-3, a3=1e-3, noise_dbfs=-103, quant_bits=19))
            else:
                if dev == "sim":
                    ap.error("укажи --dac/--adc или добавь --sim")
                be = AlsaCli(hw_out=dac_hw, hw_in=adc_hw,
                             in_map=cfg["in_map"], out_map=cfg["out_map"])
            validate_pair_rate(dac_hw, adc_hw, a.rate or cfg["fs"])
            rig = Rig(be, cfg, fs=a.rate or cfg["fs"], blocks=a.blocks, settle=0.25)
            rig.open()
            if not a.sim:
                split_clock_preflight(rig, dac_hw, adc_hw, level_dbfs=a.clock_level)
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
        _dac_hw, adc_hw = selected_audio_pair(a, cal)
        be = AlsaCli(hw_out=adc_hw, hw_in=adc_hw,
                     in_map=cal["in_map"], out_map=cal["out_map"])
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
        res = m.clock_check(rig, level_dbfs=a.clock_level); text = m.text_clock(res)
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
