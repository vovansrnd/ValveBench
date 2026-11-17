#!/usr/bin/env python3
"""v2.18.8 — сопоставление SNR-метрик на идентичных записях (§22 п.1).

Пары на ОДНИХ сигналах:
  * диагностический v2188_estimators.est_lsq().snr_carrier_db
    (Hann-остаток, локальный пол ±32 Гц, исключение ±2 Гц, интеграл
    по всем бинам полосы);
  * production dsp.analyze_tone().local_snr_db (BH-блоки, Pdir,
    лоб несущей ±half_f, локальный пол = медиана ±40 Гц без
    лепестка/гармоник/сети × (2·half_f+1) = 11 бинов).

Классы: белый/цветной шум с пилотом, реальные профили линий, свип
уровня пилота, чистый синус, шум без пилота, обрыв/частичное окно,
клиппинг. Выход: попарные значения, разности, матрицы решений при
порогах 20/20, регрессия diag = a·prod + b. Вердикт: обоснован ли
порог 20 дБ или нужен отдельный порог диагностической метрики.

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
from valvebench import calib, dsp  # noqa: E402
from v2188_estimators import est_lsq  # noqa: E402
from v2187_montecarlo import (FS_MC, measured_spectra,  # noqa: E402
                              noise_colored, noise_white_corr)

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = FS_MC
N = int(2.0 * FS)
PILOT_NOM = 1000.0
PPM_TRUE = -23.5
BASE_SEED = 218800
PROD_TRESH_DB = 20.0
DIAG_TRESH_DB = 20.0


def amp(db):
    return 10 ** (db / 20.0)


def pilot_sig(level, seed, noise=None, t_len=None, drop=None):
    # ВАЖНО: вызывать с ключевыми аргументами noise/t_len/drop
    n = int(t_len * FS) if t_len else N
    t = np.arange(n) / FS
    rng = np.random.default_rng(seed)
    x = amp(level) * np.sin(2 * math.pi * PILOT_NOM *
                            (1 + PPM_TRUE * 1e-6) * t +
                            2 * math.pi * ((seed * 37) % 997) / 997)
    if noise is not None:
        x = x + noise
    if drop == "half":
        x = x.copy()
        x[n // 2:] = 0.0
    elif drop == "tail_quarter":
        x = x.copy()
        x[3 * n // 4:] = 0.0
    return x


def classes(cal, spectra):
    """(метка, сигнал, должен_быть_принят)."""
    out = []
    f_true = PILOT_NOM * (1 + PPM_TRUE * 1e-6)
    for lvl in (-72.0, -66.0, -60.0):
        for i in range(60):
            nL, _ = noise_white_corr(N, BASE_SEED + int(-lvl) * 100 + i)
            out.append((f"white{lvl:+.0f}#{i}",
                        pilot_sig(f_true, seed=BASE_SEED + 1000 + i,
                                  noise=nL), True))
    for i in range(60):
        nL, _ = noise_colored(N, BASE_SEED + 2000 + i, spectra[1],
                              spectra[2], spectra[3])
        out.append((f"colored-72#{i}",
                    pilot_sig(f_true, seed=BASE_SEED + 2100 + i, noise=nL),
                    True))
    # реальные профили линий (амплитуды/фазы из WAV)
    prof_path = OUT / "v2187-lsq-pilots.json"
    if prof_path.exists():
        meas = json.load(open(prof_path, encoding="utf-8"))["measured_profile"]
        for i in range(60):
            rng = np.random.default_rng(BASE_SEED + 3000 + i)
            t = np.arange(N) / FS
            x = pilot_sig(f_true, -72.0, BASE_SEED + 3100 + i)
            for fh, v in meas.items():
                if v["amp_L"] > 0:
                    x[:N] += v["amp_L"] * np.sin(
                        2 * math.pi * float(fh) * t +
                        2 * math.pi * rng.random())
            out.append((f"real-profile#{i}", x, True))
    for lvl in (-80.0, -90.0, -100.0, -110.0, -120.0):
        for i in range(40):
            nL, _ = noise_white_corr(N, BASE_SEED + 4000 + int(-lvl) + i)
            out.append((f"levels{lvl:+.0f}#{i}",
                        pilot_sig(f_true, seed=BASE_SEED + 4100
                                  + int(-lvl) + i, noise=nL),
                        lvl >= -90.0))
    for i in range(30):
        t = np.arange(N) / FS
        x = amp(-60.0) * np.sin(2 * math.pi * f_true * t +
                                2 * math.pi * ((i * 37) % 997) / 997)
        out.append((f"clean#{i}", x, True))
    for i in range(60):
        rng = np.random.default_rng(BASE_SEED + 5000 + i)
        out.append((f"nopilot-80#{i}",
                    amp(-80.0) / math.sqrt(2.0) * rng.standard_normal(N),
                    False))
    for i in range(60):
        rng = np.random.default_rng(BASE_SEED + 5100 + i)
        out.append((f"nopilot-96#{i}",
                    amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N),
                    False))
    for i in range(40):
        nL = amp(-96.0) / math.sqrt(2.0) * \
            np.random.default_rng(BASE_SEED + 5200 + i).standard_normal(N)
        x = pilot_sig(f_true, seed=BASE_SEED + 5300 + i, noise=nL,
                      drop="half")
        out.append((f"dropout-half#{i}", x, False))
    for i in range(40):
        x = pilot_sig(f_true, seed=BASE_SEED + 5400 + i, t_len=0.8)
        buf = np.zeros(N)
        buf[:len(x)] = x
        out.append((f"partial40#{i}", buf, False))
    for i in range(30):
        x = pilot_sig(f_true, -60.0, BASE_SEED + 5500 + i, noise=None)
        out.append((f"clip#{i}",
                    np.clip(x, -0.35 * amp(-60.0), 0.35 * amp(-60.0)),
                    False))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="/opt/MyApps/Adudio/"
                    "ValveBench-v214-clock-diagnostic/measurements/"
                    "diagnostics/v214-clock-diagnostic")
    args = ap.parse_args()
    cal = calib.Calibration()
    spectra = measured_spectra(args.source_dir, FS)
    rows = []
    for name, x, expect in classes(cal, spectra):
        qc = est_lsq(x, FS, PILOT_NOM)
        r = dsp.analyze_tone(x, FS, PILOT_NOM, N, nharm=2,
                             band=(20.0, cal["band_hi"]), mains=cal["mains"])
        prod_snr = r.get("local_snr_db")
        prod_snr = float(prod_snr) if prod_snr is not None and \
            math.isfinite(prod_snr) else None
        diag_snr = qc.get("snr_carrier_db")
        prod_dec = ("accept" if prod_snr is not None and
                    prod_snr >= PROD_TRESH_DB else "refuse")
        diag_dec = ("accept" if diag_snr is not None and
                    diag_snr >= DIAG_TRESH_DB else "refuse")
        rows.append({"name": name, "expect": "accept" if expect else "refuse",
                     "diag_status": qc["status"],
                     "diag_snr": diag_snr, "prod_snr": prod_snr,
                     "diff_diag_minus_prod": (round(diag_snr - prod_snr, 2)
                                              if diag_snr is not None and
                                              prod_snr is not None else None),
                     "prod_decision": prod_dec, "diag_decision": diag_dec,
                     "agree": prod_dec == diag_dec})
        if len(rows) % 150 == 0:
            print(f"  … {len(rows)}", flush=True)

    fin = [r for r in rows if r["diag_snr"] is not None and
           r["prod_snr"] is not None]
    diffs = np.asarray([r["diff_diag_minus_prod"] for r in fin])
    prod_v = np.asarray([r["prod_snr"] for r in fin])
    diag_v = np.asarray([r["diag_snr"] for r in fin])
    # регрессия diag = a·prod + b по конечным парам
    a, b = np.polyfit(prod_v, diag_v, 1)
    # матрица решений
    from collections import Counter
    dm = Counter((r["prod_decision"], r["diag_decision"]) for r in rows)
    # согласованность ожиданий
    errs = {"prod_wrong": sum(1 for r in rows
                              if r["prod_decision"] != r["expect"]),
            "diag_wrong": sum(1 for r in rows
                              if r["diag_decision"] != r["expect"])}
    summary = {
        "trials": len(rows),
        "pairs_finite": len(fin),
        "diff_diag_minus_prod_ppb": {
            "mean": round(float(np.mean(diffs)), 2),
            "median": round(float(np.median(diffs)), 2),
            "std": round(float(np.std(diffs)), 2),
            "min": round(float(np.min(diffs)), 2),
            "max": round(float(np.max(diffs)), 2)},
        "regression_diag_vs_prod": {"slope": round(float(a), 4),
                                    "offset_db": round(float(b), 2)},
        "decision_matrix_prod_x_diag": {
            f"prod_{k[0]}/diag_{k[1]}": v for k, v in sorted(dm.items())},
        "decision_errors_vs_expectation": errs,
        "thresholds": {"prod": PROD_TRESH_DB, "diag": DIAG_TRESH_DB},
        "verdict": None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    # расхождения решений — детали
    dis = [r for r in rows if not r["agree"]]
    summary["disagreements"] = [{"name": r["name"], "prod": r["prod_snr"],
                                 "diag": r["diag_snr"],
                                 "expect": r["expect"]}
                                for r in dis[:40]]
    if errs["prod_wrong"] == 0 and errs["diag_wrong"] == 0 and \
            abs(a - 1.0) < 0.15 and abs(b) < 3.0:
        summary["verdict"] = ("метрики согласованы (наклон ~1, смещение "
                              "малo, решения совпадают с ожиданиями) — "
                              "порог 20 дБ переносим")
    else:
        summary["verdict"] = ("метрики требуют раздельных порогов: "
                              f"diag = {a:.3f}·prod + {b:.2f}; "
                              f"ошибки решений prod {errs['prod_wrong']}, "
                              f"diag {errs['diag_wrong']}")
    print("ВЕРДИКТ:", summary["verdict"], flush=True)
    json.dump({"summary": summary, "per_trial": rows},
              open(OUT / "v2188-snr-compare.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v2188-snr-compare.json")


if __name__ == "__main__":
    import argparse
    main()
