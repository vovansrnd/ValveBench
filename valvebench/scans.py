"""Диагностические сканы тракта: по уровню и по частоте теста.

Зачем. Тон 1000 Гц на 96 кГц — точно периодический сигнал с периодом 96 отсчётов.
Квантование и внутренние шумоформирователи преобразователей для такого сигнала дают
не шум, а линии ровно на гармониках тона, причём их уровень почти не зависит от
амплитуды (для симметричной формы — только нечётные). Анализатор честно «видит» их
как гармоники, хотя нелинейность тракта тут ни при чём. Поэтому AES17 использует
997 Гц (простое число): период сигнала становится равным целой секунде, и такая
«шаблонная» ошибка размазывается по всем бинам.

levelscan  — абсолютный уровень каждой гармоники против уровня тона. Настоящая
             гармоника H_h растёт как A^h (наклон h дБ/дБ), а «шаблонная» линия
             стоит на месте (наклон ≈ 0).
freqscan   — те же линии на разных частотах теста: артефакт периодичности исчезает
             на 997 Гц; внешний спур остаётся на кратных 1000 Гц при любой f0.
"""
from __future__ import annotations

import math

import numpy as np

from . import dsp

SLOPE_TOL = 0.8             # допуск на наклон, дБ/дБ
MIN_SPAN_DB = 9.0           # диапазон уровней, нужный для оценки наклона


def tone_period_samples(f, fs):
    """Период цифрового синуса в отсчётах (целая частота) или None для нецелой."""
    fi = int(round(f))
    if abs(f - fi) > 1e-9 or fi <= 0:
        return None
    return int(round(fs / math.gcd(fi, int(fs))))


def period_note(f, fs):
    p = tone_period_samples(f, fs)
    if p is None:
        return None
    if p <= 1000:
        return (f"период тона {p} отсчётов ({1000.0 * p / fs:.2f} мс): ошибка квантования и шумоформирователей "
                f"преобразователей концентрируется в линиях на гармониках. Для измерения THD бери простую "
                f"частоту (997 Гц, как в AES17).")
    return None


def classify_slope(h, fund, absl, ok, tol=SLOPE_TOL):
    """Наклон абсолютного уровня гармоники h от уровня тона по достоверным точкам.

    Возвращает dict(slope, kind, n, rms) или None, если данных мало.
    kind: 'distortion' (наклон ≈ h), 'fixed' (≈ 0), 'mixed'."""
    idx = [i for i, o in enumerate(ok) if o and np.isfinite(absl[i])]
    if len(idx) < 3:
        return None
    x = np.array([fund[i] for i in idx], dtype=float)
    y = np.array([absl[i] for i in idx], dtype=float)
    if float(x.max() - x.min()) < MIN_SPAN_DB:
        return None
    slope, icpt = np.polyfit(x, y, 1)
    rms = float(np.sqrt(np.mean((y - (slope * x + icpt)) ** 2)))
    if abs(slope - h) <= tol:
        kind = "distortion"
    elif abs(slope) <= tol:
        kind = "fixed"
    else:
        kind = "mixed"
    return {"slope": float(slope), "kind": kind, "n": len(idx), "rms": rms}


_KIND_TEXT = {
    "distortion": "растёт с уровнем как у нелинейности",
    "fixed": "НЕ зависит от уровня: не нелинейность (периодичность/спур/смещение)",
    "mixed": "смешанная природа",
}


# ---------------------------------------------------------------- level scan


def run_level_scan(rig, frequency=997.0, levels=(-1.0, -3.0, -6.0, -9.0, -12.0, -18.0, -24.0, -30.0), nharm=9):
    levels = sorted({float(v) for v in levels}, reverse=True)
    if len(levels) < 4:
        raise ValueError("для сканирования по уровню нужно минимум 4 разных уровня")
    if any(not (-80.0 <= v <= -0.5) for v in levels):
        raise ValueError("уровни ЦАП должны быть в диапазоне −80…−0.5 dBFS")
    segs = []
    for lv in levels:
        k, a = rig.amp_tone(frequency, lv)
        segs.append(([(k, a)], [(k, a)], None))
    wins, warns = rig.run(segs)           # самый громкий сегмент идёт первым
    k = rig.amp_tone(frequency, 0.0)[0]
    f0 = rig.f_of(k)
    result = {"kind": "level_scan", "f": f0, "fs": rig.fs, "levels": levels, "warnings": list(warns), "ch": {}}
    for c, ch in ((0, "L"), (1, "R")):
        rows = []
        for lv, w in zip(levels, wins):
            r = rig._tone_an(w, c, k, nharm)
            peak = float(np.max(np.abs(w[:, c])))
            rows.append({
                "gen": lv, "fund": float(r["level_dbfs"]), "thdn_db": float(r["thdn_db"]),
                "clip": peak >= 0.98,
                "H": {int(h["h"]): (float(r["level_dbfs"] + h["raw_dbc"]), bool(h.get("credible")))
                      for h in r["harmonics"]},
            })
        slopes = {}
        for h in range(2, nharm + 1):
            pts = [(row["fund"], row["H"][h][0], row["H"][h][1] and not row["clip"])
                   for row in rows if h in row["H"]]
            if pts:
                s = classify_slope(h, [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts])
                if s:
                    slopes[h] = s
        result["ch"][ch] = {"rows": rows, "slopes": slopes}
    return result


def text_level_scan(res):
    L = [f"LEVEL SCAN · тон {res['f']:.0f} Гц · абсолютные уровни гармоник, dBFS "
         f"(* — линия достоверна, пусто — ниже порога)"]
    for ch, d in res["ch"].items():
        hs = sorted({h for row in d["rows"] for h in row["H"]})
        L.append(f"\n[{ch}]  ген    тон   " + "".join(f"{'H' + str(h):>8s}" for h in hs) + "   THD+N dBc")
        for row in d["rows"]:
            cells = ""
            for h in hs:
                v, ok = row["H"].get(h, (None, False))
                cells += f"{v:7.1f}*" if (v is not None and ok) else f"{'·':>8s}"
            clip = "  CLIP" if row["clip"] else ""
            L.append(f"   {row['gen']:6.1f} {row['fund']:6.1f}  {cells}   {row['thdn_db']:7.1f}{clip}")
        if d["slopes"]:
            L.append("   наклон абс. уровня гармоники от уровня тона (у нелинейности H_h: +h дБ/дБ):")
            for h, s in sorted(d["slopes"].items()):
                L.append(f"     H{h}: {s['slope']:+.2f} дБ/дБ (n={s['n']}, разброс {s['rms']:.1f} дБ) — {_KIND_TEXT[s['kind']]}")
        else:
            L.append("   наклоны не оценены: достоверных точек мало (подними усреднение --blocks или уровни)")
        fixed = [h for h, s in d["slopes"].items() if s["kind"] == "fixed"]
        dist = [h for h, s in d["slopes"].items() if s["kind"] == "distortion"]
        if fixed:
            L.append(f"   ВЫВОД: линии H{', H'.join(map(str, sorted(fixed)))} не зависят от уровня — это не THD тракта.")
        if dist:
            L.append(f"   ВЫВОД: H{', H'.join(map(str, sorted(dist)))} ведут себя как настоящие гармоники нелинейности.")
    note = period_note(res["f"], res.get("fs", 96000))
    if note:
        L.append("\n! " + note)
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])


# ----------------------------------------------------------------- freq scan


def _comb(rig, win_ch, f0, comb_hz=1000.0, m_max=8):
    """Линии на кратных comb_hz (кроме гармоник f0): абсолютные уровни и достоверность."""
    n, fs = rig.n, rig.fs
    M = len(win_ch) // n
    avg = np.asarray(win_ch[: M * n], dtype=float).reshape(M, n).mean(axis=0)
    P = dsp._rect_power(avg)
    df = fs / n
    skip = np.zeros(P.size, dtype=bool)
    kf = int(round(f0 / df))
    for j in range(1, 13):
        kk = j * kf
        if kk < P.size:
            skip[max(0, kk - 2): kk + 3] = True
    km = [int(round(m * comb_hz / df)) for m in range(1, m_max + 1)]
    for k in km:                                   # соседние кратные тоже не считаем «полом»
        skip[max(0, k - 2): k + 3] = True
    out = []
    for m, k in zip(range(1, m_max + 1), km):
        if k >= P.size or any(abs(k - j * kf) <= 2 for j in range(1, 13)):
            continue
        p = float(P[k])
        fl = dsp._local_floor(P, k, np.zeros(P.size, dtype=bool) | _only(skip, km, k))
        margin = dsp.db10(p / fl) if fl > 0 and p > 0 else 99.0
        out.append({"m": m, "f": k * df, "abs": float(dsp.rms_dbfs(math.sqrt(p))) if p > 0 else -300.0,
                    "margin": margin, "credible": margin >= dsp.MARGIN_OK_DB})
    return out


def _only(skip, comb_bins, k):
    """Маска пропуска без самой проверяемой линии: её окрестность ±2 бина не должна исключать пол."""
    mask = skip.copy()
    mask[max(0, k - 2): k + 3] = False
    return mask


def run_freq_scan(rig, freqs=(1000.0, 997.0, 1234.0), level=-6.0, nharm=9, comb_hz=1000.0):
    if not freqs or len(freqs) < 2:
        raise ValueError("для сканирования по частоте нужно минимум две частоты")
    if not (-80.0 <= float(level) <= -0.5):
        raise ValueError("уровень ЦАП должен быть в диапазоне −80…−0.5 dBFS")
    segs, ks = [], []
    for f in freqs:
        k, a = rig.amp_tone(f, level)
        ks.append(k)
        segs.append(([(k, a)], [(k, a)], None))
    wins, warns = rig.run(segs)
    res = {"kind": "freq_scan", "level": float(level), "comb_hz": comb_hz, "warnings": list(warns),
           "fs": rig.fs, "rows": []}
    for f, k, w in zip(freqs, ks, wins):
        f0 = rig.f_of(k)
        per = tone_period_samples(f0, rig.fs)
        for c, ch in ((0, "L"), (1, "R")):
            r = rig._tone_an(w, c, k, nharm)
            lines = {int(h["h"]): (float(r["level_dbfs"] + h["raw_dbc"]), bool(h.get("credible")))
                     for h in r["harmonics"]}
            res["rows"].append({"f": f0, "period": per, "ch": ch, "fund": float(r["level_dbfs"]),
                                "lines": lines, "comb": _comb(rig, w[:, c], f0, comb_hz)})
    return res


def freq_scan_verdict(res):
    """Короткие выводы: периодичность тона или внешний спур."""
    out = []
    fs = res.get("fs", 96000)
    for ch in ("L", "R"):
        rows = [r for r in res["rows"] if r["ch"] == ch]
        short = [r for r in rows if r["period"] is not None and r["period"] <= 1000]
        long_ = [r for r in rows if r["period"] is None or r["period"] >= fs // 2]
        if short and long_:
            n_s = max(sum(1 for v in r["lines"].values() if v[1]) for r in short)
            n_l = max(sum(1 for v in r["lines"].values() if v[1]) for r in long_)
            lv_s = [v[0] for r in short for v in r["lines"].values() if v[1]]
            lv_l = [v[0] for r in long_ for v in r["lines"].values() if v[1]]
            if n_s >= 3 and n_l <= n_s / 2:
                drop = (np.mean(lv_s) - np.mean(lv_l)) if lv_l else None
                tail = f"; средний уровень линий упал на {drop:.0f} дБ" if drop is not None else ""
                out.append(f"[{ch}] линии гармоник на периодическом тоне ({n_s} достоверных) исчезают на "
                           f"непериодичном (остались {n_l}){tail}: это артефакт периодичности, а не THD тракта.")
            elif n_s >= 3:
                out.append(f"[{ch}] линии остаются и на непериодичном тоне ({n_l} из {n_s}): "
                           "искажения/спуры реально присутствуют в тракте.")
        comb_l = [r for r in long_ if r["comb"]]
        if comb_l:
            cc = sum(1 for r in comb_l for x in r["comb"] if x["credible"])
            if cc >= 2:
                out.append(f"[{ch}] линии на кратных {res['comb_hz']:.0f} Гц сохраняются при f0, не кратной 1 кГц: "
                           "внешний спур (кадры USB, питание), а не гармоники.")
    return out


def text_freq_scan(res):
    L = [f"FREQ SCAN · уровень ЦАП {res['level']:.1f} dBFS · абсолютные уровни, dBFS (* — достоверна)"]
    for r in res["rows"]:
        per = f"{r['period']} отсч." if r["period"] else "нецелая частота"
        hs = " ".join((f"H{h}:{v:6.1f}{'*' if ok else ' '}") for h, (v, ok) in sorted(r["lines"].items()))
        L.append(f"[{r['ch']}] f0={r['f']:7.1f} Гц  период {per:>14s}  тон {r['fund']:.2f}")
        L.append(f"      гармоники: {hs}")
        comb = [x for x in r["comb"] if x["credible"]]
        if comb:
            L.append("      на кратных 1 кГц (не гармоники f0): " + " ".join(f"{x['f'] / 1000:g}k:{x['abs']:.1f}" for x in comb))
    verdict = freq_scan_verdict(res)
    L += ["", "ВЫВОДЫ:"] + (["  " + v for v in verdict] if verdict else ["  однозначного вывода нет (нужен периодичный и непериодичный тон, --blocks 8)."])
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])
