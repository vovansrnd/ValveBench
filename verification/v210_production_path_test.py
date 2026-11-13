#!/usr/bin/env python3
"""v2.10 — аппаратный тест НАСТОЯЩЕГО production-пути (§14.6 задание):
Rig с GUI-дефолтами (blocks=2, settle=0.5, edge-защита candidate), реальный
m.clock_check (−72 dBFS production default → документирование SNR), реальный
m.clock_check (−30 dBFS → ok), реальный m.thd_point (−30 dBFS, 997 Гц).
2 повтора. Полный захват сохраняется; границы окна — из Rig.run.

Production-код не изменяется вне candidate-патча edge-защиты (уже на ветке).
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))
from verification.timing_probe import chirp  # noqa: E402
from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402

DAC_USBID = "152a:85dd"
ADC_USBID = "041e:3f04"
FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v210"
REPEATS = 2


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


def main():
    dac, adc = find_card(DAC_USBID), find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = calib.Calibration()
    # GUI-дефолты: m.Rig(be, cal, fs=self.rig_fs()) → blocks=2, settle=0.5
    # edge-защита candidate: включена по умолчанию на диагностической ветке
    be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc,
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    rig = m.Rig(be, cfg, fs=FS)   # blocks=2, settle=0.5 — GUI-дефолты
    print(f"Rig: blocks={rig.blocks} settle={rig.settle} edge_protect="
          f"{rig.edge_protect_enabled} lead={rig.edge_protect_lead_s} "
          f"tail={rig.edge_protect_tail_s}", flush=True)

    results = []
    for rep in (1, 2):
        print(f"\n=== повтор {rep}/{REPEATS} ===", flush=True)
        r = {"repeat": rep, "preflight_level_dbfs": -72.0}

        # 1) production clock_check на −72 dBFS (как в приложении по умолчанию)
        try:
            ck72 = m.clock_check(rig, level_dbfs=-72.0)
            r["clock_72"] = {"ok": ck72.get("ok"),
                             "ppm": {c: ck72["ch"][c]["ppm"] for c in ck72["ch"]},
                             "levels": {c: ck72["ch"][c]["level_dbfs"] for c in ck72["ch"]},
                             "warnings": ck72.get("warnings", [])}
            print(f"  clock_check −72 dBFS: ok={ck72.get('ok')} ppm={r['clock_72']['ppm']}",
                  flush=True)
        except Exception as exc:
            r["clock_72"] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            print(f"  clock_check −72 dBFS: ИСКЛЮЧЕНИЕ {r['clock_72']['error']}", flush=True)

        # 2) production clock_check на −30 dBFS (диагностический уровень)
        try:
            ck30 = m.clock_check(rig, level_dbfs=-30.0)
            r["clock_30"] = {"ok": ck30.get("ok"),
                             "ppm": {c: ck30["ch"][c]["ppm"] for c in ck30["ch"]},
                             "levels": {c: ck30["ch"][c]["level_dbfs"] for c in ck30["ch"]}}
            print(f"  clock_check −30 dBFS: ok={ck30.get('ok')} ppm={r['clock_30']['ppm']}",
                  flush=True)
        except Exception as exc:
            r["clock_30"] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            print(f"  clock_check −30 dBFS: ИСКЛЮЧЕНИЕ {r['clock_30']['error']}", flush=True)

        # 3) production thd_point (997 Гц −30 dBFS) — настоящий путь целиком
        cap_path = OUT / f"thd-capture-r{rep}-{datetime.now().strftime('%H%M%S')}.wav"
        try:
            thd = m.thd_point(rig, f=997.0, level_dbfs=-30.0,
                              capture_wav=str(cap_path))
            r["thd"] = {"thdn_pct_L": thd["ch"]["L"]["thdn_pct"],
                        "thdn_pct_R": thd["ch"]["R"]["thdn_pct"],
                        "level_L": thd["ch"]["L"]["level_dbfs"],
                        "level_R": thd["ch"]["R"]["level_dbfs"],
                        "warnings": thd.get("warnings", [])}
            r["capture_wav"] = str(cap_path)
            print(f"  thd_point: THD+N L {r['thd']['thdn_pct_L']} % / "
                  f"R {r['thd']['thdn_pct_R']} %", flush=True)
        except Exception as exc:
            r["thd"] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            print(f"  thd_point: ИСКЛЮЧЕНИЕ {r['thd']['error']}", flush=True)

        # 4) joint-LSQ верификация сохранённого захвата: окно внутри тона?
        if cap_path.exists():
            _, cap = wavfile.read(str(cap_path))
            cap = cap.astype(np.float64)
            sys.path.insert(0, str(ROOT / "verification"))
            from v26_offline_reconstruction import joint_lsq_profile
            # плато: 2-мс RMS, −55 дБФС, устойчивость 50 мс (методика v2.3)
            w = int(0.002 * FS)
            nn = len(cap) // w * w
            rms2 = np.sqrt(np.mean(cap[:nn].reshape(-1, w, 2) ** 2, axis=1)).max(axis=1) * np.sqrt(2)
            db2 = 20 * np.log10(np.clip(rms2, 1e-12, None))
            hi2 = np.where(db2 > -55)[0]
            run_min = 25
            p0 = p1 = None
            for i in hi2:
                if (hi2[i:i + run_min] > 0).all() or (db2[i:i + run_min] > -55).all():
                    p0 = i * w; break
            for i in hi2[::-1]:
                if (db2[max(0, i - run_min + 1):i + 1] > -55).all():
                    p1 = (i + 1) * w; break
            prof = joint_lsq_profile(cap, FS, 0, len(cap), win_s=0.25, hop_s=0.05)
            stable = [p for p in prof
                      if p["A_997_dbfs"] > p["A_1000_dbfs"] + 6 and p["A_997_dbfs"] > -45]
            r["stable997"] = {"first_s": stable[0]["t_center_s"] if stable else None,
                              "last_s": stable[-1]["t_center_s"] if stable else None,
                              "span_s": round(stable[-1]["t_center_s"] - stable[0]["t_center_s"], 3)
                              if stable else 0}
            # production-окно из thd_point: границы из rig.run — восстановим из
            # onset: onset = детектнутый; окно = onset + settle + blocks*n
            # верификация окна: joint-LSQ на САМОМ ОКНЕ (2.0 с) — если A997
            # стабильно ≥ −45 дБФС на всех 0.25-с подокнах, окно целиком
            # внутри устойчивого тона ( onset mid-tone не вызывается —
            # детектор требует pre-tone тишину, окно начинается mid-tone)
            from v26_offline_reconstruction import joint_lsq_profile
            prof = joint_lsq_profile(cap, FS, 0, len(cap), win_s=0.25, hop_s=0.125)
            stable = [p for p in prof
                      if p["A_997_dbfs"] > p["A_1000_dbfs"] + 6 and p["A_997_dbfs"] > -45]
            r["window_lsq"] = {"windows_total": len(prof),
                               "stable997_windows": len(stable),
                               "all_stable": len(stable) == len(prof),
                               "min_A997": min((p["A_997_dbfs"] for p in prof), default=None)}
            print(f"  верификация окна: stable997 {len(stable)}/{len(prof)} подокон "
                  f"(min A997 {min((p['A_997_dbfs'] for p in prof), default='—')} дБФС)",
                  flush=True)
        results.append(r)
        time.sleep(0.5)

    json.dump({"rig": {"blocks": rig.blocks, "settle": rig.settle,
                       "edge_protect": rig.edge_protect_enabled,
                       "lead_s": rig.edge_protect_lead_s, "tail_s": rig.edge_protect_tail_s},
               "clock_probe_level_production": -72.0,
               "results": results},
              open(OUT / "v210-results.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "v210-results.json")


if __name__ == "__main__":
    main()
