#!/usr/bin/env python3
"""v2.18.7 — систематическая калибровка line-QC: свип уровня/смещения
линий + негативы без пилота (журнал §21 п.5).

Сетка позитивов: уровень линии ОТНОСИТЕЛЬНО НЕСУЩЕЙ +20…−45 дБc
(включая окрестность порога −35 дБc), смещения ±1, ±1.5, ±2, ±3, ±5 Гц,
пилот 997 Гц −72 дБFS, белый шум −96 дБFS; плюс реальные измеренные
профили линий (амплитуды и фазы из v2186-lsq-pilots.json, источник WAV).

Негативы: тон −72 + белый шум; чистый синус; шум −80/−96 БЕЗ пилота;
НЧ-тяжёлый шум без пилота; цветной line-free шум с пилотом.

Для каждого случая: обнаружение (статус), правильность атрибуции
(line_hz в ±1.5 Гц от истинной), ошибка частоты если принято, причина
отказа. TP/FP/TN/FN с размерами выборки. Слепая зона ±1.5 Гц
документируется: помехи внутри зоны исключаются QC ПО КОНСТРУКЦИИ
(проектная граница, не свойство шума). Нулевые ошибки на выборке не
означают нулевую частоту ошибок в реальном тракте.

Playback НЕТ; production только импортируется.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2187_estimators import est_lsq  # noqa: E402
from v2187_montecarlo import (FS_MC, measured_spectra,  # noqa: E402
                              noise_colored, noise_white_corr)

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = FS_MC
N = int(2.0 * FS)
PILOT_NOM = 997.0
PPM_TRUE = -23.5
BASE_SEED = 218750
TRIALS_PER_CELL = 30
LEVELS_DBC = (20.0, 10.0, 0.0, -10.0, -20.0, -30.0, -35.0, -40.0, -45.0)
OFFSETS_HZ = (1.0, -1.0, 1.5, -1.5, 2.0, -2.0, 3.0, -3.0, 5.0, -5.0)


def amp(db):
    return 10 ** (db / 20.0)


def synth_trial(f_pilot, pilot_lvl, lines, seed, noise):
    """lines = [(f_line, amp, phase)]; шум добавляется снаружи."""
    t = np.arange(N) / FS
    x = amp(pilot_lvl) * np.sin(2 * math.pi * f_pilot * t +
                                2 * math.pi * ((seed * 37) % 997) / 997)
    for fl, a, ph in lines:
        x = x + a * np.sin(2 * math.pi * fl * t + ph)
    return x + noise


def main():
    spectra = measured_spectra(
        "/opt/MyApps/Adudio/ValveBench-v214-clock-diagnostic/measurements/"
        "diagnostics/v214-clock-diagnostic", FS)
    f_pilot = PILOT_NOM * (1 + PPM_TRUE * 1e-6)
    pilot_amp = amp(-72.0)
    recs = []
    negs = []

    # --- НЕГАТИВЫ (интерференции нет): 6 классов × 60
    for i in range(60):
        nL, _ = noise_white_corr(N, 601000 + i)
        x = synth_trial(f_pilot, -72.0, [], 601000 + i, nL)
        negs.append(("white+pilot", x))
    for i in range(60):
        t = np.arange(N) / FS
        x = amp(-60.0) * np.sin(2 * math.pi * f_pilot * t +
                                2 * math.pi * ((i * 37) % 997) / 997)
        negs.append(("clean", x))
    for i in range(60):
        rng = np.random.default_rng(604000 + i)
        negs.append(("white-nopilot-80",
                     amp(-80.0) / math.sqrt(2.0) * rng.standard_normal(N)))
    for i in range(60):
        rng = np.random.default_rng(605000 + i)
        negs.append(("white-nopilot-96",
                     amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)))
    for i in range(60):
        rng = np.random.default_rng(606000 + i)
        w = np.fft.rfft(amp(-80.0) / math.sqrt(2.0) *
                        rng.standard_normal(N))
        frq = np.arange(len(w)) * (FS / N)
        w = w / np.sqrt(1.0 + (frq / 200.0) ** 2)
        negs.append(("lf-nopilot", np.fft.irfft(w, N)))
    for i in range(60):
        nL, _ = noise_colored(N, 607000 + i, spectra[1], spectra[2],
                              spectra[3])
        x = synth_trial(f_pilot, -72.0, [], 607000 + i, nL)
        negs.append(("colored+pilot", x))

    for name, x in negs:
        qc = est_lsq(x, FS, PILOT_NOM)
        cls = "TN" if qc["status"] == "ok" else (
            "FP_line" if qc["status"] == "line_contaminated" else
            f"FP_other:{qc['status']}")
        recs.append({"cell": name, "verdict": cls, "status": qc["status"],
                     "line_hz": qc.get("line_hz"),
                     "line_dbc": qc.get("line_dbc"),
                     "f_est": (round(qc["f_est"], 4)
                               if qc.get("f_est") is not None else None),
                     "f_line": None, "snr": qc.get("snr_carrier_db")})
    print(f"  негативы: {len(recs)}", flush=True)

    # --- ПОЗИТИВЫ: сетка уровень(дБc)×смещение (белый шум)
    for dbc in LEVELS_DBC:
        line_amp = pilot_amp * amp(dbc)
        for off in OFFSETS_HZ:
            f_line = f_pilot + off
            cell = f"dbc{dbc:+.0f} off{off:+.1f}"
            for tr in range(TRIALS_PER_CELL):
                seed = BASE_SEED + int((dbc + 50) * 1000) + \
                    int((off + 10) * 100) + tr
                rng = np.random.default_rng(seed)
                nL, _ = noise_white_corr(N, seed + 91)
                ph = 2 * math.pi * rng.random()
                x = synth_trial(f_pilot, -72.0, [(f_line, line_amp, ph)],
                                seed, nL)
                qc = est_lsq(x, FS, PILOT_NOM)
                cls = ("FN" if qc["status"] == "ok" else
                       ("TP" if qc["status"] == "line_contaminated" else
                        f"TP_unnamed:{qc['status']}"))
                attr = (qc.get("line_hz") is not None and
                        abs(qc["line_hz"] - f_line) <= 1.5)
                err = ((qc["f_est"] / f_pilot - 1) * 1e6 - PPM_TRUE
                       if qc.get("f_est") is not None else None)
                recs.append({"cell": cell, "verdict": cls,
                             "status": qc["status"],
                             "line_hz": qc.get("line_hz"),
                             "line_dbc": qc.get("line_dbc"),
                             "f_est": (round(qc["f_est"], 4)
                                       if qc.get("f_est") is not None
                                       else None),
                             "f_line": round(f_line, 3),
                             "attr_ok": bool(attr),
                             "err_ppm_if_accepted":
                                 (round(err, 2) if err is not None
                                  and qc["status"] == "ok" else None)})
        print(f"  сетка: уровень {dbc:+.0f} дБc готов", flush=True)

    # --- ПОЗИТИВЫ: реальные линии профиля (амплитуды/фазы из WAV)
    prof_path = OUT / "v2186-lsq-pilots.json"
    if prof_path.exists():
        prof = json.load(open(prof_path, encoding="utf-8"))
        meas = prof["measured_profile"]
        rel = [(fh, v["amp_L"] / pilot_amp)
               for fh, v in meas.items() if v["amp_L"] > 0]
        dbc_list = sorted({round(20 * math.log10(max(r, 1e-12)), 1)
                           for _, r in rel}, reverse=True)
        for i in range(30):
            rng = np.random.default_rng(700000 + i)
            lines = [(f_pilot + (fh - 1000.0), a * pilot_amp,
                      2 * math.pi * rng.random()) for fh, a in rel]
            nL, _ = noise_white_corr(N, 701000 + i)
            x = synth_trial(f_pilot, -72.0, lines, 700000 + i, nL)
            qc = est_lsq(x, FS, PILOT_NOM)
            recs.append({"cell": "real-profile",
                         "verdict": ("FN" if qc["status"] == "ok" else
                                     "TP" if qc["status"] ==
                                     "line_contaminated" else
                                     f"TP_unnamed:{qc['status']}"),
                         "status": qc["status"],
                         "line_hz": qc.get("line_hz"),
                         "line_dbc": qc.get("line_dbc"),
                         "f_est": (round(qc["f_est"], 4)
                                   if qc.get("f_est") is not None else None),
                         "f_line": None,
                         "profile_dbc_range": [dbc_list[0], dbc_list[-1]],
                         "profile_n_lines": len(rel)})
        print("  реальные профили: готово", flush=True)

    # --- сводка
    def cnt(pred):
        return sum(1 for r in recs if pred(r))
    n_pos = cnt(lambda r: r["verdict"].startswith("TP") or
                r["verdict"] == "FN")
    n_neg = cnt(lambda r: r["cell"] in
                ("clean", "white+pilot", "white-nopilot-80",
                 "white-nopilot-96", "lf-nopilot", "colored+pilot"))
    summary = {
        "trials_total": len(recs),
        "positives": n_pos,
        "TP": cnt(lambda r: r["verdict"] == "TP"),
        "TP_named_of_TP": cnt(lambda r: r["verdict"] == "TP" and
                              r.get("attr_ok")),
        "TP_unnamed": cnt(lambda r: r["verdict"].startswith("TP_unnamed")),
        "FN": cnt(lambda r: r["verdict"] == "FN"),
        "negatives": n_neg,
        "TN": cnt(lambda r: r["verdict"] == "TN"),
        "FP_line": cnt(lambda r: r["verdict"] == "FP_line"),
        "FP_other": cnt(lambda r: r["verdict"].startswith("FP_other")),
        "note": ("нулевые ошибки на выборке не означают нулевую частоту "
                 "ошибок в реальном тракте; слепая зона ±1.5 Гц — "
                 "проектная граница исключения вокруг несущей"),
    }
    summary["recall"] = (round((summary["TP"] + summary["TP_unnamed"]) /
                               n_pos, 4) if n_pos else None)
    summary["false_positive_rate"] = (round((summary["FP_line"] +
                                             summary["FP_other"]) / n_neg, 4)
                                      if n_neg else None)
    grid = {}
    for r in recs:
        c = r["cell"]
        if c.startswith("dbc") or c == "real-profile":
            g = grid.setdefault(c, {"n": 0, "refused": 0, "attr_ok": 0,
                                    "accepted": 0, "err_accepted": [],
                                    "fn": 0})
            g["n"] += 1
            if r["verdict"] == "FN":
                g["accepted"] += 1
                g["fn"] += 1
                if r.get("err_ppm_if_accepted") is not None:
                    g["err_accepted"].append(r["err_ppm_if_accepted"])
            elif r["verdict"] == "TP":
                g["refused"] += 1
                g["attr_ok"] += int(r.get("attr_ok", False))
            else:
                g["refused"] += 1
    summary["grid"] = {k: {"n": v["n"], "refused": v["refused"],
                           "attr_ok": v["attr_ok"],
                           "accepted": v["accepted"], "FN": v["fn"],
                           "err_accepted_ppm": (round(float(np.mean(
                               np.abs(v["err_accepted"]))), 1)
                               if v["err_accepted"] else None)}
                       for k, v in sorted(grid.items())}
    print(json.dumps({k: v for k, v in summary.items() if k != "grid"},
                     ensure_ascii=False, indent=1), flush=True)
    print("ячейки с принятыми (FN) или неполной атрибуцией:", flush=True)
    for k, v in summary["grid"].items():
        if v["accepted"] or v["attr_ok"] < v["refused"]:
            print(f"  {k}: {v}", flush=True)
    json.dump({"summary": summary, "per_trial": recs},
              open(OUT / "v2187-qc-sweep.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v2187-qc-sweep.json")



if __name__ == "__main__":
    main()
