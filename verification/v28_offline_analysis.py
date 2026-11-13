#!/usr/bin/env python3
"""v2.8 — офлайн-анализ восьми сохранённых захватов v2.7 (§14 задание
координатора, п.1-2/4). Без железа, без изменения production.

Для каждого захвата (v26ab-capture-{i}-{cond}.wav):
  * joint-LSQ профиль (окно 0.25 с, шаг 0.125 с, ОБА канала) двух синусоид:
    996.977 Гц (программный тон, ppm −23.5) и 999.977 Гц (остаток preflight
    1 кГц) + DC — амплитуды и остаток в дБФС;
  * устойчивый тон: A997 ≥ −45 дБФС, A997 ≥ A1000 + 6 дБ (разделение
    997/1000), устойчивость ≥30 мс — первый/последний кадр;
  * потеря тона: ожидаемый конец (плато-старт + 4.54 с) − последний
    устойчивый кадр;
  * RMS: старт/середина/последняя секунда/после тона;
  * уровни на −45/−55/−65 дБФС (устойчивость 20 мс) с кадрами.

Ожидаемые позиции (база захвата): aplay старт ≈ 0.5 с (arecord pre-roll),
пад 0.05 с, lead 0/1.5 с, fade-in 0.01 с → старт тона ≈ 0.56+lead.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
DIAG = ROOT / "measurements" / "diagnostics"
FS = 192000
MEASURED_PPM = -23.5
F_TONE = 997.0 * (1 + MEASURED_PPM * 1e-6)      # 996.9766
F_RES = 1000.0 * (1 + MEASURED_PPM * 1e-6)      # 999.9765
CAPS = [(1, "L0", 0.0), (2, "L1.5", 1.5), (3, "L1.5", 1.5),
        (4, "L0", 0.0), (5, "L0", 0.0), (6, "L1.5", 1.5),
        (7, "L1.5", 1.5), (8, "L0", 0.0)]


def demod_env(x, fs, f, win_s=0.001):
    n = len(x)
    t = np.arange(n) / fs
    z = x * np.exp(-2j * np.pi * f * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(win_s * fs)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    return 2 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))


def joint_lsq_windows(cap, fs, win_s=0.25, hop_s=0.125, f1=F_TONE, f2=F_RES):
    out = []
    win = int(round(win_s * fs))
    hop = int(round(hop_s * fs))
    tt = np.arange(win) / fs
    D = np.column_stack([
        np.sin(2 * np.pi * f1 * tt), np.cos(2 * np.pi * f1 * tt),
        np.sin(2 * np.pi * f2 * tt), np.cos(2 * np.pi * f2 * tt),
        np.ones(win)])
    for i in range(0, len(cap) - win, hop):
        seg = cap[i:i + win]
        coefs, res = [], []
        for c in (0, 1):
            coef, *_ = np.linalg.lstsq(D, seg[:, c], rcond=None)
            resid = seg[:, c] - D @ coef
            coefs.append(coef)
            res.append(float(np.sqrt(np.mean(resid ** 2)) * np.sqrt(2)))
        a1 = 20 * np.log10(max(float(np.hypot(coefs[0][0], coefs[0][1])), 1e-12))
        a2 = 20 * np.log10(max(float(np.hypot(coefs[0][2], coefs[0][3])), 1e-12))
        out.append({"t_center_s": round((i + win / 2) / fs, 3),
                    "A997_L": round(a1, 1), "A997_R": round(
                        20 * np.log10(max(float(np.hypot(coefs[1][0], coefs[1][1])), 1e-12)), 1),
                    "A1000_L": round(a2, 1),
                    "A1000_R": round(20 * np.log10(max(float(np.hypot(coefs[1][2], coefs[1][3])), 1e-12)), 1),
                    "resid_L": round(res[0], 1), "resid_R": round(res[1], 1)})
    return out


def main():
    all_results = []
    for idx, cond, lead_s in CAPS:
        cap_path = DIAG / "v26-ab" / f"v26ab-capture-{idx}-{cond}.wav"
        fsx, cap = wavfile.read(cap_path)
        cap = cap.astype(np.float64)
        n_cap = len(cap)
        # ожидаемые позиции (база захвата): aplay старт ≈ 0.5 с (arecord pre-roll),
        # пад 0.05 с, fade-in 0.01 с → старт тона
        prog_start = 0.5 + 0.05
        tone_start_exp = prog_start + lead_s + 0.01
        tone_end_exp = tone_start_exp + 4.54          # 4.55 минус fade-in

        # плато: env997 выше −45 дБФС с устойчивостью 30 мс (ОТНОСИТЕЛЬНАЯ
        # длительность — надёжна); абсолютные границы головы/хвоста на этих
        # захватах НЕ измеряются корректно: aplay-старт в базе захвата не
        # записан, pre-tone область содержит остаток preflight (1 кГц),
        # триггерящий порог демодуляции — разделение только joint-LSQ
        e997L = demod_env(cap[:, 0], FS, 997)
        e997R = demod_env(cap[:, 1], FS, 997)
        thr = 10 ** (-45 / 20)
        run_min = int(0.03 * FS)

        def stable_bounds(e):
            above = np.where(e > thr)[0]
            st = en = None
            if len(above):
                for i in above:
                    if (e[i:i + run_min] > thr).all():
                        st = i; break
                for i in above[::-1]:
                    if (e[max(0, i - run_min + 1):i + 1] > thr).all():
                        en = i + 1; break
            return st, en

        sL, eL = stable_bounds(e997L)
        sR, eR = stable_bounds(e997R)
        tone_start = max(x for x in (sL, sR) if x is not None)
        tone_end = min(x for x in (eL, eR) if x is not None)
        loss = round(4.55 - (tone_end - tone_start) / FS, 4)   # относительная

        # уровни на порогах −45/−55/−65 (устойчивое падение 20 мс после тона)
        drops = {}
        for tdb in (-45, -55, -65):
            t = 10 ** (tdb / 20)
            below = e997R < t
            d = None
            for i in range(tone_start, n_cap - int(0.02 * FS)):
                if below[i:i + int(0.02 * FS)].all():
                    d = i; break
            drops[f"below_{abs(tdb)}"] = round(d / FS, 4) if d is not None else None

        r_tone_mid = 20 * np.log10(max(float(np.sqrt(np.mean(
            cap[tone_start + int(0.5*FS):tone_start + int(1.5*FS)]**2)) * np.sqrt(2)), 1e-12))
        r_last1s = 20 * np.log10(max(float(np.sqrt(np.mean(
            cap[tone_end - FS:tone_end]**2)) * np.sqrt(2)), 1e-12))
        r_post = 20 * np.log10(max(float(np.sqrt(np.mean(
            cap[min(n_cap - int(0.4*FS), tone_end):min(n_cap - int(0.4*FS), tone_end) + int(0.4*FS)]**2))
            * np.sqrt(2)), 1e-12))

        rec = {"run": idx, "cond": cond, "lead_s": lead_s,
               "stable_tone_start_s": round(tone_start / FS, 4),
               "stable_tone_end_s": round(tone_end / FS, 4),
               "stable_len_s": round((tone_end - tone_start) / FS, 4),
               "loss_vs_submitted_s": round(4.55 - (tone_end - tone_start) / FS, 4),
               "rms_tone_mid": round(r_tone_mid, 1),
               "rms_last_1s": round(r_last1s, 1),
               "rms_post_0_4s": round(r_post, 1),
               "drops_R_s": drops,
               "joint_lsq_profile": joint_lsq_windows(cap, FS),
               }
        all_results.append(rec)
        print(f"[{idx} {cond}] плато {rec['stable_tone_start_s']}..{rec['stable_tone_end_s']} "
              f"({rec['stable_len_s']} с) | потеря {rec['loss_vs_submitted_s']} с "
              f"| RMS тон {rec['rms_tone_mid']} посл.1с {rec['rms_last_1s']} "
              f"после {rec['rms_post_0_4s']}", flush=True)

    print("\n=== СВОДКА потерь (относительная: 4.55 − плато) ===")
    for cond in ("L0", "L1.5"):
        rows = [r for r in all_results if r["cond"] == cond]
        print(f"{cond}: плато {[r['stable_len_s'] for r in rows]} "
              f"потеря {[r['loss_vs_submitted_s'] for r in rows]}")
    out = DIAG / "v26-ab" / "v28-offline-analysis.json"
    json.dump({"fs": FS, "measured_ppm": MEASURED_PPM, "f_tone": F_TONE,
               "f_res": F_RES, "results": all_results},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("JSON:", out)


if __name__ == "__main__":
    main()
