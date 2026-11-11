"""glitch_test без ALSA: подставные устройства «пишут» программу воспроизведения с внесёнными дефектами."""
import os, sys, tempfile, math, json, csv
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from pathlib import Path
from valvebench import glitch_test as gt, steady
from valvebench.pcm import from_pcm, to_pcm

ok = 0
def check(name, cond, info=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + name + (f": {info}" if info else ""))
    if not cond: raise SystemExit(1)
    ok += 1

FS = 96000
DEFECTS = {}
def fake_choose(dev, fs, direction, requested="auto"):
    return steady.DeviceFormat(dev, "S24_3LE", 2)
def fake_capture_and_play(play_file, capture_file, play, capture, fs, capture_seconds, lead_seconds=0.5):
    """«Запись» = программа воспроизведения + шум АЦП + заданные дефекты в L."""
    x = from_pcm(Path(play_file).read_bytes(), play.fmt, 2)
    rng = np.random.default_rng(5)
    cap = np.vstack([np.zeros((int(lead_seconds * fs), 2)), x, np.zeros((int(2.0 * fs), 2))])
    cap = cap + 2e-7 * rng.standard_normal(cap.shape)
    for t, kind in DEFECTS.get("L", []):
        i = int((lead_seconds + t) * fs)
        if kind == "single": cap[i, 0] += 4e-4
        if kind == "dropout": cap[i:i + int(0.086 * fs), 0] = 0.0
    Path(capture_file).write_bytes(to_pcm(np.clip(cap, -1, 1), capture.fmt))
    d = Path(tempfile.mkdtemp())
    (d / "arecord.stderr").write_text("")
    (d / "aplay.stderr").write_text("underrun!!! (at least 12.5 ms long)\n")
    return {"capture_stderr": str(d / "arecord.stderr"), "playback_stderr": str(d / "aplay.stderr"), "err_dir": str(d)}
steady.choose_device_format = fake_choose
steady._capture_and_play = fake_capture_and_play

# опыт A: играет L; в записи L — 5 одиночных сбоев и один провал 86 мс; в R — тишина и шум
DEFECTS["L"] = [(10.0, "single"), (14.0, "single"), (14.001, "single"), (25.0, "single"), (31.5, "single"), (22.0, "dropout")]
out = Path(tempfile.mkdtemp())
lines = []
d = gt.run_glitch_test("hw:9,0", "hw:9,0", fs=FS, channel="L", tone_level_dbfs=-18.0, seconds=40.0, idle_s=4.0,
                       out_root=out, print_fn=lines.append)
res = json.loads((d / "glitch.json").read_text())
L, R = res["adc"]["L"], res["adc"]["R"]
check("опыт A: найдены все внесённые одиночные сбои в L (5), не больше",
      L["single_sample"] == 5, f"найдено {L['single_sample']}")
check("провал несущей 86 мс найден на ведомом L с длительностью ±4 мс",
      len(L["dropouts"]) == 1 and abs(L["dropouts"][0]["duration_ms"] - 86) < 4, str(L["dropouts"]))
check("неведомый канал R: ни сбоев, ни ложных «остановок несущей», в отчёте пояснено",
      R["glitches"] == 0 and not R["dropouts"] and any("не ищутся" in ln for ln in lines if ln.startswith("ADC R")))
check("XRUN ЦАП из stderr разобран (с длительностью в мс)", res["xrun"]["playback"][0]["ms"] == 12.5)
rows = list(csv.DictReader(open(d / "events.csv", encoding="utf-8")))
check("events.csv: одиночные сбои с положительным знаком и амплитудой ≈ −68 dBFS (4e-4)",
      sum(1 for r in rows if r["class"] == "single_sample") == 5
      and all(r["sign"] == "1" for r in rows if r["class"] == "single_sample")
      and all(abs(float(r["level_dbfs"]) + 67.96) < 1.0 for r in rows if r["class"] == "single_sample"))
check("группы: два сбоя на расстоянии 1 мс — одна группа (4 группы из 5 сбоев)", L["glitch_clusters"] == 4, str(L["glitch_clusters"]))

# опыт D: тишина — тот же сбой в записи виден как «в тишине», провалы не ищутся
DEFECTS["L"] = [(8.0, "single")]
d2 = gt.run_glitch_test("hw:9,0", "hw:9,0", fs=FS, channel="none", seconds=20.0, idle_s=4.0, out_root=out, print_fn=lambda s: None)
res2 = json.loads((d2 / "glitch.json").read_text())
check("опыт D (тишина): сбой найден без тона; провалов несущей нет",
      res2["adc"]["L"]["single_sample"] == 1 and not res2["adc"]["L"]["dropouts"], str(res2["adc"]["L"]["single_sample"]))
print("\n" + "\n".join(lines[-4:]))
print(f"\n{ok} проверок пройдено")

# ---- память: пик не зависит от длины записи (файл читается окнами)
import tracemalloc
from valvebench import glitch_test as _gt
def make_capture_file(path, fs, seconds, glitch_at):
    """S24_3LE, тон 997 Гц в L (-18 dBFS), шум; пишется по секундам — генератор сам память не раздувает."""
    rng = np.random.default_rng(9)
    with open(path, "wb") as fh:
        for s0 in range(0, int(seconds * fs), fs):
            n = min(fs, int(seconds * fs) - s0)
            t = (np.arange(s0, s0 + n)) / fs
            x = np.column_stack([0.126 * np.sin(2 * np.pi * 997.0 * t), np.zeros(n)]) + 3e-7 * rng.standard_normal((n, 2))
            for g in glitch_at:
                if s0 <= g < s0 + n: x[g - s0, 0] += 5e-4
            fh.write(to_pcm(np.clip(x, -1, 1), "S24_3LE"))
peaks = {}
for secs in (30, 90):
    fs_m = 192000
    d = Path(tempfile.mkdtemp()); f = d / "cap.raw"
    make_capture_file(f, fs_m, secs, [int(7.3 * fs_m), int((secs - 9.0) * fs_m)])
    src = _gt._FileSource(f, "S24_3LE", 2)
    tracemalloc.start()
    outd = d / "out"; outd.mkdir()
    total_s = secs
    # «программа»: lead 0, idle 3 с, fade 0.5 с, тон до конца минус хвост
    _gt.analyse_recording(src, fs_m, "L", -18.0, seconds=secs - 8.0, tone_hz=997.0, idle_s=3.0, fade_s=0.5, lead=0.0,
                          total_s=secs, xr={"playback": [], "capture": []}, outdir=outd, print_fn=lambda s: None)
    peaks[secs] = tracemalloc.get_traced_memory()[1] / 2 ** 20
    tracemalloc.stop()
    res_m = json.loads((outd / "glitch.json").read_text())
    check(f"{secs} с @192 кГц из файла: найдено 2 внесённых одиночных сбоя", res_m["adc"]["L"]["single_sample"] == 2,
          str(res_m["adc"]["L"]["single_sample"]))
raw_mib = lambda secs: secs * 192000 * 2 * 3 / 2 ** 20
f64_mib = lambda secs: secs * 192000 * 2 * 8 / 2 ** 20
check("пиковая память остаётся ниже 150 МиБ на записях 30–90 с",
      peaks[90] < 1.3 * peaks[30] and peaks[90] < 150.0,
      f"пик {peaks[30]:.0f} МиБ (30 с) / {peaks[90]:.0f} МиБ (90 с); раньше только массив float64 на 90 с — {f64_mib(90):.0f} МиБ, "
      f"на 10 мин — {f64_mib(600):.0f} МиБ при файле {raw_mib(600):.0f} МиБ")
print(f"\n{ok} проверок пройдено")
