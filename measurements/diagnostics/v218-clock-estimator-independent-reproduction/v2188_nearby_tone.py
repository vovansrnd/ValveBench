#!/usr/bin/env python3
"""v2.18.8 — валидация nearby-tone дискриминатора (§22 п.3, ДИАГНОСТИКА).

Принцип (v2188_estimators.nearby_tone_probe): остаток демодулируется на
f_est; настоящий соседний тон на смещении Δ (внутри исключения ±1.5 Гц)
даёт огибающую |z(t)|, осциллирующую на |Δ| Гц; структурная утечка
неидеального фита даёт огибающую без компонент в 0.3–1.6 Гц. Спектр
огибающей (Ханн, децимация до 64 Гц) → пик ≥ 3× медиана.

Классы:
  * чистый пилот −60 (без шума) — биений быть не должно;
  * пилот + белый шум −96 — некогерентный фон;
  * синхронная/свободная соседняя линия: Δ ∈ {0.5, 1.0, 1.5} Гц ×
    уровни {−10, −20, −30, −40} дБc × 20;
  * реальные профили линий (нет линий в ±1.5 Гц) — биений не должно.

Валидационные поля: найдено ли биение, nearby_hz ≈ Δ (±0.5 Гц бин),
nearby_db ≈ dbc (±2 дБ). Статусы оценщика НЕ меняются — дискриминатор
диагностический. Граница исключения ±1.5 Гц не меняется.

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
from v2188_estimators import est_lsq  # noqa: E402
from v2187_montecarlo import noise_white_corr  # noqa: E402

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = FS_MC = 48000.0
N = int(2.0 * FS)
PILOT_NOM = 997.0
PPM_TRUE = -23.5
BASE_SEED = 218810
TRIALS = 20


def amp(db):
    return 10 ** (db / 20.0)


def main():
    f_pilot = PILOT_NOM * (1 + PPM_TRUE * 1e-6)
    pilot_amp = amp(-72.0)
    recs = []
    # 1) чистый пилот без шума — биений быть не должно
    for i in range(20):
        t = np.arange(N) / FS
        x = pilot_amp * np.sin(2 * math.pi * f_pilot * t +
                               2 * math.pi * ((i * 37) % 997) / 997)
        qc = est_lsq(x, FS, PILOT_NOM)
        recs.append({"cell": "clean-pilot", "verdict": "quiet" if
                     not qc.get("nearby_tone_hz") else "FIRED",
                     "probe_hz": qc.get("nearby_tone_hz"),
                     "probe_db": qc.get("nearby_tone_db"),
                     "true_delta": None, "true_dbc": None})
    # 2) пилот + белый шум
    for i in range(20):
        nL, _ = noise_white_corr(N, BASE_SEED + i)
        t = np.arange(N) / FS
        x = pilot_amp * np.sin(2 * math.pi * f_pilot * t +
                               2 * math.pi * ((i * 37) % 997) / 997) + nL
        qc = est_lsq(x, FS, PILOT_NOM)
        recs.append({"cell": "pilot+noise", "verdict": "quiet" if
                     not qc.get("nearby_tone_hz") else "FIRED",
                     "probe_hz": qc.get("nearby_tone_hz"),
                     "probe_db": qc.get("nearby_tone_db"),
                     "true_delta": None, "true_dbc": None})
    # 3) соседняя линия внутри исключения: Δ × дБc × (свободная/синхронная)
    for sync in (False, True):
        for delta in (0.5, 1.0, 1.5):
            for dbc in (-10.0, -20.0, -30.0, -40.0):
                for tr in range(TRIALS):
                    seed = BASE_SEED + 1000 + int((delta + 10) * 100) + \
                        int(-dbc) * 10 + tr
                    rng = np.random.default_rng(seed)
                    t = np.arange(N) / FS
                    ph_pilot = 2 * math.pi * rng.random()
                    ph_line = ph_pilot if sync else 2 * math.pi * rng.random()
                    x = pilot_amp * np.sin(2 * math.pi * f_pilot * t +
                                           ph_pilot)
                    x = x + pilot_amp * amp(dbc) * np.sin(
                        2 * math.pi * (f_pilot + delta) * t + ph_line)
                    nL, _ = noise_white_corr(N, seed + 91)
                    qc = est_lsq(x + nL, FS, PILOT_NOM)
                    hz = qc.get("nearby_tone_hz")
                    fired = hz is not None
                    f_line = f_pilot + delta
                    ok_hz = fired and abs(hz - f_line) <= 0.6
                    ok_db = (fired and qc.get("nearby_tone_db") is not None
                             and abs(qc["nearby_tone_db"] - dbc) <= 2.5)
                    recs.append({"cell": f"line d{delta:+.1f} {dbc:+.0f}dBc "
                                         f"{'sync' if sync else 'free'}",
                                 "verdict": ("detected" if fired and ok_hz
                                             else
                                             "missed" if not fired else
                                             "wrong_hz"),
                                 "probe_hz": hz, "probe_db":
                                     qc.get("nearby_tone_db"),
                                 "true_delta": delta, "true_dbc": dbc,
                                 "ok_hz": ok_hz, "ok_db": ok_db,
                                 "lsq_status": qc["status"]})
    # 4) реальные профили (нет линий в ±1.5 Гц) — биений не должно
    prof_path = OUT / "v2187-lsq-pilots.json"
    if prof_path.exists():
        meas = json.load(open(prof_path, encoding="utf-8"))["measured_profile"]
        for i in range(20):
            rng = np.random.default_rng(BASE_SEED + 7000 + i)
            t = np.arange(N) / FS
            x = pilot_amp * np.sin(2 * math.pi * f_pilot * t +
                                   2 * math.pi * ((i * 37) % 997) / 997)
            for fh, v in meas.items():
                if v["amp_L"] > 0:
                    x += v["amp_L"] * np.sin(2 * math.pi * float(fh) * t +
                                             2 * math.pi * rng.random())
            nL, _ = noise_white_corr(N, BASE_SEED + 7100 + i)
            qc = est_lsq(x + nL, FS, PILOT_NOM)
            recs.append({"cell": "real-profile", "verdict": "quiet" if
                         not qc.get("nearby_tone_hz") else "FIRED",
                         "probe_hz": qc.get("nearby_tone_hz"),
                         "probe_db": qc.get("nearby_tone_db"),
                         "true_delta": None, "true_dbc": None})

    # сводка
    def cnt(pred):
        return sum(1 for r in recs if pred(r))
    line_recs = [r for r in recs if r.get("true_delta") is not None]
    quiet_recs = [r for r in recs if r.get("true_delta") is None]
    summary = {
        "trials": len(recs),
        "negative_cells": {
            "quiet_expected": len(quiet_recs),
            "quiet_correct": cnt(lambda r: r.get("true_delta") is None and
                                 r["verdict"] == "quiet"),
            "false_fired": cnt(lambda r: r.get("true_delta") is None and
                               r["verdict"] == "FIRED")},
        "line_cells": {
            "total": len(line_recs),
            "detected_hz_ok": cnt(lambda r: r.get("verdict") == "detected"),
            "missed": cnt(lambda r: r.get("verdict") == "missed"),
            "wrong_hz": cnt(lambda r: r.get("verdict") == "wrong_hz"),
            "db_ok_of_detected": cnt(lambda r: r.get("ok_db"))},
        "per_cell": {}}
    for r in line_recs:
        c = r["cell"].rsplit(" ", 1)[0]
        g = summary["per_cell"].setdefault(c, {"n": 0, "detected": 0,
                                               "ok_hz": 0, "ok_db": 0})
        g["n"] += 1
        g["detected"] += int(r["verdict"] == "detected")
        g["ok_hz"] += int(r.get("ok_hz", False))
        g["ok_db"] += int(r.get("ok_db", False))
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    json.dump({"summary": summary, "per_trial": recs},
              open(OUT / "v2188-nearby-tone.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v2188-nearby-tone.json")


if __name__ == "__main__":
    main()
