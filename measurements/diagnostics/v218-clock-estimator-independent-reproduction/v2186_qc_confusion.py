#!/usr/bin/env python3
"""v2.18.6 — калибровка line-QC: TP/FP/TN/FN (журнал §20.7 п.2/3).

Негативы (интерференции НЕТ → ожидается ok):
  * чистый синус (номинал + уход D10 −23.5 ppm, без шума);
  * белый коррелированный шум (−80/−96 дБFS) × уровни −72/−66/−60;
  * цветной шум (line-free модель из захватов) × те же уровни.
Позитивы (интерференция ВНЕСЕНА → ожидается отказ с атрибуцией):
  * точный контрпример v2.18.4: пилот 997 + линия 1000 Гц −30 дБFS;
  * сетка сдвигов ±3/±5 Гц × уровни −30/−40/−50/−60 дБFS (белый шум);
  * цветной шум + линия +3 Гц (−30 и −50 дБFS).
Дополнительно (п.3): ЛЕГАСИ-оценщик 8d8b040 на контрпримере —
воспроизведение исторического смещения −127 ppm.

Классификация: TP = позитив отклонён (line_hz в ±2 Гц от истинной
помехи — «named», иначе «unnamed»); FN = позитив принят (ok);
FP = негатив отклонён (line_contaminated — «FP_line», иные отказы —
«FP_other»); TN = негатив принят.
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
from valvebench import dsp  # noqa: E402
from v2186_estimators import est_lsq  # noqa: E402
from v2186_montecarlo import (FS_MC, noise_colored,  # noqa: E402
                              noise_white_corr, measured_spectra)
from v2185_868_reproduction import est_lsq_8d8b040  # noqa: E402  (легаси)

OUT = ROOT / "measurements" / "diagnostics" / "v218-clock-estimator-independent-reproduction"
FS = FS_MC
N = int(2.0 * FS)
PILOT_NOM = 1000.0
PPM_TRUE = -23.5
PILOT_POS = 997.0            # позитивные сценарии на 997 (как в v2.18.4)
BASE_SEED = 218650


def amp(dbfs):
    return 10 ** (dbfs / 20.0)


def tone_x(f_true, level, seed, noise=None, phase=None):
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    ph = phase if phase is not None else 2 * math.pi * ((seed * 37) % 997) / 997
    x = amp(level) * np.sin(2 * math.pi * f_true * t + ph)
    if noise is not None:
        x = x + noise
    return x


def trial_negatives():
    """Негативы: (метка, сигнал, f_nom)."""
    out = []
    f_true = PILOT_NOM * (1 + PPM_TRUE * 1e-6)
    for i in range(100):
        out.append((f"clean#{i}",
                    tone_x(f_true, -60.0, BASE_SEED + i, noise=None),
                    PILOT_NOM))
    for li, level in enumerate((-72.0, -66.0, -60.0)):
        for i in range(100):
            seed = BASE_SEED + 1000 + li * 100 + i
            nL, _ = noise_white_corr(N, seed)
            out.append((f"white{level:+.0f}#{i}",
                        tone_x(f_true, level, seed + 77, noise=nL),
                        PILOT_NOM))
    return out


def trial_negatives_colored(spectra):
    out = []
    f_true = PILOT_NOM * (1 + PPM_TRUE * 1e-6)
    for li, level in enumerate((-72.0, -66.0, -60.0)):
        for i in range(100):
            seed = BASE_SEED + 5000 + li * 100 + i
            nL, _ = noise_colored(N, seed, spectra[1], spectra[2], spectra[3])
            out.append((f"colored{level:+.0f}#{i}",
                        tone_x(f_true, level, seed + 77, noise=nL),
                        PILOT_NOM))
    return out


def trial_positives(spectra):
    """Позитивы: пилот 997 + линия рядом. (метка, сигнал, f_nom, f_line)"""
    out = []
    f_pilot = PILOT_POS * (1 + PPM_TRUE * 1e-6)
    # точный контрпример v2.18.4: 997 + 1000 Гц −30 дБFS
    for i in range(100):
        seed = BASE_SEED + 20000 + i
        rng = np.random.default_rng(seed)
        t = np.arange(N) / FS
        x = amp(-72.0) * np.sin(2 * math.pi * f_pilot * t +
                                2 * math.pi * rng.random())
        x = x + amp(-30.0) * np.sin(2 * math.pi * f_pilot * (1000.0 / 997.0) *
                                    t + 2 * math.pi * rng.random())
        nL, _ = noise_white_corr(N, seed + 91)
        out.append((f"counterexample#{i}", x + nL, PILOT_POS, 1000.0))
    # сетка сдвигов × уровней (белый шум)
    for off in (+3.0, -3.0, +5.0, -5.0):
        for level in (-30.0, -40.0, -50.0, -60.0):
            for i in range(50):
                seed = BASE_SEED + 30000 + int((off + 10) * 1000) + \
                    int(-level) * 100 + i
                rng = np.random.default_rng(seed)
                t = np.arange(N) / FS
                f_line = PILOT_POS * (1 + PPM_TRUE * 1e-6) + off
                x = amp(-72.0) * np.sin(2 * math.pi * f_pilot * t +
                                        2 * math.pi * rng.random())
                x = x + amp(level) * np.sin(2 * math.pi * f_line * t +
                                            2 * math.pi * rng.random())
                nL, _ = noise_white_corr(N, seed + 91)
                out.append((f"white off{off:+.0f} {level:+.0f}#{i}", x + nL,
                            PILOT_POS, f_line))
    # цветной шум + линия
    for level in (-30.0, -50.0):
        for i in range(50):
            seed = BASE_SEED + 60000 + int(-level) * 100 + i
            rng = np.random.default_rng(seed)
            t = np.arange(N) / FS
            f_line = PILOT_POS * (1 + PPM_TRUE * 1e-6) + 3.0
            x = amp(-72.0) * np.sin(2 * math.pi * f_pilot * t +
                                    2 * math.pi * rng.random())
            x = x + amp(level) * np.sin(2 * math.pi * f_line * t +
                                        2 * math.pi * rng.random())
            nL, _ = noise_colored(N, seed + 91, spectra[1], spectra[2],
                                  spectra[3])
            out.append((f"colored off+3 {level:+.0f}#{i}", x + nL,
                        PILOT_POS, f_line))
    return out


def classify(name, qc, is_positive, f_line=None):
    st = qc["status"]
    if is_positive:
        if st == "ok":
            return "FN"
        if st == "line_contaminated" and f_line is not None and \
                qc.get("line_hz") is not None and \
                abs(qc["line_hz"] - f_line) <= 2.0:
            return "TP_named"
        return "TP_unnamed"
    if st == "line_contaminated":
        return "FP_line"
    if st == "ok":
        return "TN"
    return "FP_other"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="/opt/MyApps/Adudio/"
                    "ValveBench-v214-clock-diagnostic/measurements/"
                    "diagnostics/v214-clock-diagnostic")
    args = ap.parse_args()
    spectra = measured_spectra(args.source_dir, FS)
    print(f"цветная модель line-free: уплощено "
          f"{spectra[5]['n_bins_flattened']}", flush=True)
    trials = trial_negatives() + trial_negatives_colored(spectra) + \
        trial_positives(spectra)
    rows = []
    for name, x, f_nom, *rest in trials:
        f_line = rest[0] if rest else None
        qc = est_lsq(x, FS, f_nom)
        verdict = classify(name, qc, f_line is not None, f_line)
        rows.append({"name": name, "verdict": verdict, "status": qc["status"],
                     "line_hz": qc.get("line_hz"),
                     "line_dbc": qc.get("line_dbc"),
                     "f_est_hz": (round(qc["f_est"], 4)
                                  if qc.get("f_est") is not None else None),
                     "f_line": f_line})
        if len(rows) % 200 == 0:
            print(f"  … {len(rows)}/{len(trials)}", flush=True)
    # сводка
    def cnt(pred):
        return sum(1 for r in rows if pred(r))
    summary = {
        "trials": len(rows),
        "TP_named": cnt(lambda r: r["verdict"] == "TP_named"),
        "TP_unnamed": cnt(lambda r: r["verdict"] == "TP_unnamed"),
        "FN": cnt(lambda r: r["verdict"] == "FN"),
        "TN": cnt(lambda r: r["verdict"] == "TN"),
        "FP_line": cnt(lambda r: r["verdict"] == "FP_line"),
        "FP_other": cnt(lambda r: r["verdict"] == "FP_other"),
    }
    n_pos = summary["TP_named"] + summary["TP_unnamed"] + summary["FN"]
    n_neg = summary["TN"] + summary["FP_line"] + summary["FP_other"]
    summary["positives"] = n_pos
    summary["negatives"] = n_neg
    summary["recall"] = (round((summary["TP_named"] + summary["TP_unnamed"])
                               / n_pos, 3) if n_pos else None)
    summary["false_positive_rate"] = (round((summary["FP_line"] +
                                             summary["FP_other"]) / n_neg, 3)
                                      if n_neg else None)
    # детализация FP (на каких негативах и что «нашло»)
    fps = [r for r in rows if r["verdict"].startswith("FP")]
    summary["FP_detail"] = [{"name": r["name"], "status": r["status"],
                             "line_hz": r["line_hz"],
                             "line_dbc": r["line_dbc"]} for r in fps[:20]]
    # FN detail
    fns = [r for r in rows if r["verdict"] == "FN"]
    summary["FN_detail"] = [{"name": r["name"], "status": r["status"],
                             "line_hz": r["line_hz"]} for r in fns[:20]]
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    # п.3: легаси 8d8b040 на контрпримере — историческое −127 ppm
    legacy = []
    for i in range(100):
        seed = BASE_SEED + 20000 + i
        rng = np.random.default_rng(seed)
        t = np.arange(N) / FS
        f_pilot = PILOT_POS * (1 + PPM_TRUE * 1e-6)
        x = amp(-72.0) * np.sin(2 * math.pi * f_pilot * t +
                                2 * math.pi * rng.random())
        x = x + amp(-30.0) * np.sin(2 * math.pi * f_pilot * (1000.0 / 997.0) *
                                    t + 2 * math.pi * rng.random())
        nL, _ = noise_white_corr(N, seed + 91)
        f_est, _ = est_lsq_8d8b040(x + nL, FS, PILOT_POS)
        err_ppm = (f_est / PILOT_POS - 1) * 1e6 - PPM_TRUE
        legacy.append(round(err_ppm, 1))
    leg = {"n": len(legacy), "err_ppm_min": min(legacy),
           "err_ppm_max": max(legacy),
           "err_ppm_median": float(np.median(legacy)),
           "err_ppm_mean": round(float(np.mean(legacy)), 1),
           "note": "легаси 8d8b040 БЕЗ guard/QC: смещение оценки помехой "
                   "1000 Гц −30 дБFS (исторический контрпример v2.18.4)"}
    print("ЛЕГАСИ −127 ppm:", json.dumps(leg, ensure_ascii=False), flush=True)
    json.dump({"summary": summary, "legacy_counterexample": leg,
               "per_trial": rows},
              open(OUT / "v2186-qc-confusion.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v2186-qc-confusion.json")


if __name__ == "__main__":
    main()
