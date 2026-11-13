#!/usr/bin/env python3
"""v2.9 — офлайн-анализ восьми захватов v2.7 частотно-селективным методом
(§14.5.1-3 координатора).

Основной метод границ — СКОЛЬЗЯЩИЙ JOINT-LSQ: окно 0.5 с, шаг 0.05 с (90 %
перекрытие), совместный МНК-фит синусоид 996.977 Гц (программный тон, ppm
−23.5) и 999.977 Гц (остаток preflight 1 кГц) + DC, ОБА канала. Каждое окно
классифицируется: stable997 / preflight-residue / transition / silence.
Границы устойчивого тона = границы непрерывной stable997-полосы (с
допуском одиночных transition-окон между stable-окнами). Переходные окна
помечаются ОТДЕЛЬНО — не объявляются потерянными кадрами.

Дополнительно: resid в dBFS (корректное преобразование), обусловленность
матрицы дизайна, положение границ относительно поданной программы.

1-мс демодуляция — вспомогательный график, не источник границ.
"""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
DIAG = ROOT / "measurements" / "diagnostics" / "v26-ab"
FS = 192000
MEASURED_PPM = -23.5
F_TONE = 997.0 * (1 + MEASURED_PPM * 1e-6)     # 996.9766
F_RES = 1000.0 * (1 + MEASURED_PPM * 1e-6)     # 999.9765
WIN_S = 0.5
HOP_S = 0.05
# фактический cadence v2.7: arecord старт (capture t=0), sleep 0.5, aplay
# (stdin), пад 0.05 — зафиксировано в скрипте v26_targeted_ab.py
APLAY_START_S = 0.5
CAPS = [(1, "L0", 0.0), (2, "L1.5", 1.5), (3, "L1.5", 1.5),
        (4, "L0", 0.0), (5, "L0", 0.0), (6, "L1.5", 1.5),
        (7, "L1.5", 1.5), (8, "L0", 0.0)]
# фактический cadence v2.7: arecord старт, sleep 0.5, aplay (stdin), пад 0.05
# (production sleep 0.30 — в v2.7-скрипте 0.5; расхождение зафиксировано)
APLAY_START_S = 0.5
PAD_S = 0.05


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


def sliding_lsq(cap, fs, win_s=0.5, hop_s=0.05):
    """Скользящий joint-LSQ: (профили, обусловленность)."""
    win = int(round(win_s * fs))
    hop = int(round(hop_s * fs))
    tt = np.arange(win) / fs
    D = np.column_stack([
        np.sin(2 * np.pi * F_TONE * tt), np.cos(2 * np.pi * F_TONE * tt),
        np.sin(2 * np.pi * F_RES * tt), np.cos(2 * np.pi * F_RES * tt),
        np.ones(win)])
    # обусловленность матрицы дизайна (нормированных колонок)
    Dn = D / np.linalg.norm(D, axis=0)
    sv = np.linalg.svd(Dn, compute_uv=False)
    cond = float(sv[0] / sv[-1])
    out = []
    for i in range(0, len(cap) - win, hop):
        seg = cap[i:i + win]
        entry = {"t_start_s": round(i / fs, 3),
                 "t_center_s": round((i + win / 2) / fs, 3)}
        for c, ch in ((0, "L"), (1, "R")):
            coef, *_ = np.linalg.lstsq(D, seg[:, c], rcond=None)
            resid = seg[:, c] - D @ coef
            a997 = 20 * np.log10(max(float(np.hypot(coef[0], coef[1])), 1e-12))
            a1000 = 20 * np.log10(max(float(np.hypot(coef[2], coef[3])), 1e-12))
            r_dbfs = 20 * np.log10(max(float(np.sqrt(np.mean(resid ** 2)) * np.sqrt(2)), 1e-12))
            entry[f"A997_{ch}"] = round(a997, 1)
            entry[f"A1000_{ch}"] = round(a1000, 1)
            entry[f"resid_dBFS_{ch}"] = round(r_dbfs, 1)
        out.append(entry)
    return out, cond


def classify(entries):
    """Классификация окон: stable997 / residue / transition / silence."""
    cls = []
    for e in entries:
        a997 = max(e["A997_L"], e["A997_R"])
        a1000 = max(e["A1000_L"], e["A1000_R"])
        if a997 >= -45 and a997 >= a1000 + 3:
            c = "stable997"
        elif a1000 >= -45 and a1000 >= a997 + 3:
            c = "residue1000"
        elif a997 >= -45 or a1000 >= -45:
            c = "transition"
        else:
            c = "silence"
        cls.append({"t_center_s": e["t_center_s"], "class": c,
                    "A997": a997, "A1000": a1000})
    return cls


def stable_run_bounds(cls, max_gap_s=0.11):
    """Непрерывная stable997-полоса: допустимы одиночные transition/residue
    окна внутри (меньше max_gap_s между соседними stable-окнами)."""
    stable = [c for c in cls if c["class"] == "stable997"]
    if not stable:
        return None, None, 0
    first = stable[0]["t_center_s"]
    last = stable[-1]["t_center_s"]
    # проверка непрерывности: разрывы между stable-окнами ≤ max_gap_s
    gaps = []
    prev = first
    for c in stable[1:]:
        gap = c["t_center_s"] - prev
        if gap > max_gap_s:
            gaps.append((round(prev, 3), round(c["t_center_s"], 3), round(gap, 3)))
        prev = c["t_center_s"]
    return first, last, gaps


def main():
    results = []
    print("=== v2.9: скользящий joint-LSQ (окно 0.5 с, шаг 0.05 с) ===\n", flush=True)
    for idx, cond, lead_s in CAPS:
        cap_path = DIAG / f"v26ab-capture-{idx}-{cond}.wav"
        fsx, cap = wavfile.read(cap_path)
        cap = cap.astype(np.float64)
        n_cap = len(cap)

        entries, cond_num = sliding_lsq(cap, FS, WIN_S, HOP_S)
        cls = classify(entries)

        # границы устойчивого тона (с допустимыми одиночными переходами)
        st_first, st_last, gaps = stable_run_bounds(cls)
        # ожидаемые позиции: aplay старт = 0.5 с (sleep в скрипте), пад 0.05,
        # lead 0/1.5, fade 0.01 → тон; ПОДанная программа: тон [pad+lead+0.01,
        # pad+lead+4.54] в submitted-базе; capture-base = submitted + aplay_start
        exp_tone_start = APLAY_START_S + PAD_S + lead_s + 0.01
        exp_tone_end = exp_tone_start + 4.53          # 4.55 − 2×fade 0.01
        # переходные окна между stable-полосой и ожидаемыми границами
        trans = [c for c in cls if c["class"] == "transition"]

        rec = {
            "run": idx, "cond": cond, "lead_s": lead_s,
            "design_cond_number": round(cond_num, 2),
            "windows_total": len(entries),
            "class_counts": {c: sum(1 for x in cls if x["class"] == c)
                             for c in ("stable997", "residue1000", "transition", "silence")},
            "stable_tone_first_s": st_first,
            "stable_tone_last_s": st_last,
            "stable_run_gaps_over_110ms": gaps,
            "expected_tone_start_s": round(exp_tone_start, 4),
            "expected_tone_end_s": round(exp_tone_end, 4),
            "expected_fadeout_start_s": round(exp_tone_end, 4),
            "submitted_tone_len_s": 4.55,
            "measured_997_span_s": round(st_last - st_first, 4) if st_first else None,
            "loss_vs_submitted_s": round(4.55 - (st_last - st_first), 4) if st_first else None,
            "transition_windows": trans,
            "joint_lsq_profile": entries,
        }
        # RMS-зоны (для полноты): старт/середина/последняя секунда/после тона
        ts, te = int(exp_tone_start * FS), int(exp_tone_end * FS)
        for label, a, b in (("start", ts, ts + FS), ("mid", ts + FS, te - FS),
                            ("last_1s", te - FS, te), ("post", te, min(te + FS, n_cap))):
            if 0 <= a and b <= n_cap and b > a:
                rr = 20 * np.log10(max(float(np.sqrt(np.mean(
                    cap[a:b, 1] ** 2)) * np.sqrt(2)), 1e-12))
                rec[f"rms_{label}_dBFS_R"] = round(rr, 1)

        results.append(rec)
        stable_span = f"{st_first:.2f}..{st_last:.2f} с" if st_first else "—"
        print(f"[{idx} {cond}] stable997: {stable_span} "
              f"(классы: {rec['class_counts']}) | "
              f"потеря vs поданного: {rec['loss_vs_submitted_s']} с | "
              f"transition-окон: {len(trans)}", flush=True)

    out = DIAG / "v29-offline-analysis.json"
    # путь уже содержит v26-ab; повторное соединение убрано
    json.dump({"fs": FS, "win_s": WIN_S, "hop_s": HOP_S, "ppm": MEASURED_PPM,
               "f_tone": round(F_TONE, 4), "f_res": round(F_RES, 4),
               "aplay_start_s": APLAY_START_S, "results": results},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nJSON:", out)


if __name__ == "__main__":
    main()
