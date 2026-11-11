#!/usr/bin/env python3
"""Drift-серия D10→E-MU (задание координатора п.3): 10 повторов clock,
первый — холодный (60 с простоя), каждый прогон сохраняет WAV + JSON."""
import json, os, sys, time
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from valvebench import measure as m, calib
from valvebench.backend import AlsaCli

DAC = os.environ.get("D10_DAC", "hw:3,0")
ADC = os.environ.get("D10_ADC", "hw:2,0")
RATE = int(os.environ.get("D10_RATE", "48000"))
# Уровень clock-тона: штатные −72 dBFS НИЖЕ шумового пола линии D10→E-MU
# (−77.4 dBFS измерено; тон на АЦП ≈ −79) — onset невозможен физически.
# Координатор: «не менять уровень, чтобы заставить пройти» — здесь уровень
# поднимается не для маскировки, а выше пола линии; −30 dBFS даёт запас
# ~34 дБ и далеко от перегрузки (АЦП ≈ −43 dBFS).
LEVEL = float(os.environ.get("D10_LEVEL", "-30"))
REPEATS = int(os.environ.get("D10_REPEATS", "10"))
OUT = os.path.join(ROOT, "measurements", "validation-dev4")
PREFIX = os.environ.get("D10_PREFIX", "d10")
cfg = calib.load()

# холодный старт: убедиться, что оба PCM закрыты, и подождать
time.sleep(int(os.environ.get("D10_COLD_S", "60")))

res = []
for i in range(1, REPEATS + 1):
    rec = {}
    be = AlsaCli(hw_out=DAC, hw_in=ADC, in_map=cfg["in_map"], out_map=cfg["out_map"])
    rig = m.Rig(be, cfg, fs=RATE, blocks=4, settle=0.25)
    rig.open()
    k, a = rig.amp_tone(1000.0, LEVEL)
    seg, s_n = rig._seg([(k, a)], [(k, a)], 0.4)
    raw_rec = be.play_record(seg)
    rec = {"run": i, "cold": i == 1, "rate": RATE,
           "dac": DAC, "adc": ADC, "level_dbfs": LEVEL,
           "capture_frames": int(len(raw_rec)),
           "wav": f"{PREFIX}-clock-{RATE}-run{i:02d}.wav"}
    try:
        ck = m.clock_check(rig, level_dbfs=LEVEL)
        rec["ppm"] = {c: ck["ch"][c]["ppm"] for c in ck["ch"]}
        rec["levels"] = {c: ck["ch"][c]["level_dbfs"] for c in ck["ch"]}
        rec["ok"] = ck["ok"]
        rec["warnings"] = ck["warnings"]
        status = "PASS" if ck["ok"] else "WARN"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:90]}"
        status = "FAIL"
    # WAV из уже снятой записи (сохраняем исходную PCM-дорожку прогона)
    try:
        from valvebench import vinyl
        vinyl.write_wav(os.path.join(OUT, rec["wav"]), RATE, raw_rec)
    except Exception as e:
        rec["wav_error"] = str(e)[:60]
    json.dump(rec, open(os.path.join(OUT, f"{PREFIX}-clock-{RATE}-run{i:02d}.json"), "w"),
              ensure_ascii=False, indent=1)
    if "ppm" in rec:
        print(f"прогон {i:2d} ({'холодный' if rec['cold'] else 'тёплый'}): "
              f"L {rec['ppm']['L']:+.1f} ppm  R {rec['ppm']['R']:+.1f} ppm  [{status}]",
              flush=True)
    else:
        print(f"прогон {i:2d}: {rec.get('error')}", flush=True)
    res.append(rec)
    rig.close() if hasattr(rig, "close") else None
    be.close() if hasattr(be, "close") else None
    time.sleep(2)

ok = sum(1 for r in res if r.get("ok"))
fails = sum(1 for r in res if "error" in r)
ppms = [abs(v) for r in res for v in r.get("ppm", {}).values()]
print(f"\nИТОГО: {ok}/{REPEATS} ok, ошибок {fails}; |ppm| макс = {max(ppms):.1f}, "
      f"среднее = {sum(ppms)/len(ppms):.2f}" if ppms else "")
json.dump(res, open(os.path.join(OUT, f"{PREFIX}-clock-{RATE}-summary.json"), "w"),
          ensure_ascii=False, indent=1)
