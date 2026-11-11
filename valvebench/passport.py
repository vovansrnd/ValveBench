"""Паспорт измерительного тракта и сравнение «до/после» доработки железа.

Одна команда снимает фиксированный набор показателей на выбранных частотах дискретизации
и сохраняет JSON; вторая сравнивает два паспорта и говорит по каждому показателю — лучше,
хуже или в пределах разброса. Разброс между повторами входит в критерий: нестабильная
наводка не выдаётся за улучшение, если одно удачное измерение случайно оказалось тихим.

Показатели (на каждый канал АЦП):
  тишина       — шум по полосам 20–20k / 20k–40k / 40k–Fs/2, A-вес, фон 50 Гц, DC, линии-спуры;
  тон 997 Гц   — на −1 и −12 dBFS, несколько повторов: уровень, H2/H3 (абсолютные), негармонический
                 остаток (медиана, min…max, разброс), его раскладка по полосам и главные линии;
  проникновение — L→R и R→L.
«Негармонический остаток» — всё, что осталось после вычитания тона и гармоник: шум, боковые
полосы, посторонние линии. У доработанного входа он должен совпадать с шумом тишины.
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np
from scipy import signal as sg
from scipy.ndimage import median_filter

from . import dsp

SCHEMA = 2
TONE_HZ = 997.0
EXCESS_TOL_DB = 1.0


# ------------------------------------------------------------------ вспомогательное


def _bands_silence(fs):
    hi = fs / 2.0 * 0.999
    b = [("20–20k", 20.0, 20000.0), ("20k–40k", 20000.0, min(40000.0, hi))]
    if hi > 40000.0:
        b.append(("40k–Fs/2", 40000.0, hi))
    return b


def _bands_residual(fs):
    hi = fs / 2.0 * 0.999
    b = [("20–500", 20.0, 500.0), ("0.5–1.5k", 500.0, 1500.0), ("1.5–5k", 1500.0, 5000.0),
         ("5–20k", 5000.0, 20000.0)]
    if hi > 20000.0:
        b.append(("20k–Fs/2", 20000.0, hi))
    return b


def find_lines(block, fmin=20.0, fmax=None, thresh_db=15.0, top=6, exclude=None):
    """Узкие линии над медианным полом блока dsp.Block. exclude — булева маска бинов, где линии не ищем."""
    P, f = block.P, block.freq
    fmax = fmax or (f[-1] * 0.98)
    win = max(51, int(400.0 / block.df) | 1)
    floor = median_filter(P, size=min(win, P.size // 2 * 2 - 1), mode="nearest") / 0.693
    ok = (f >= fmin) & (f <= fmax)
    if exclude is not None:
        ok &= ~exclude
    cand = ok & (P > floor * 10 ** (thresh_db / 10.0))
    out = []
    idx = np.flatnonzero(cand)
    used = np.zeros(P.size, dtype=bool)
    for k in idx[np.argsort(P[idx])[::-1]]:
        if used[k]:
            continue
        a, b = max(0, k - 3), min(P.size, k + 4)
        used[a:b] = True
        lobe = float(np.sum(P[a:b]))
        out.append({"f": float(f[k]), "dbfs": float(dsp.rms_dbfs(math.sqrt(lobe))),
                    "margin_db": float(10 * math.log10(P[k] / max(floor[k], 1e-30)))})
        if len(out) >= top:
            break
    return out


def capture_silence(rig, seconds, settle=1.5):
    n = int((seconds + settle) * rig.fs)
    rec = rig.be.play_record(np.zeros((n, 2)), tail=0.0)
    a = int(settle * rig.fs)
    return rec[a:a + int(seconds * rig.fs)], list(rig.be.notes)


def analyze_silence(x, fs, mains=50.0):
    res = {}
    for c, ch in ((0, "L"), (1, "R")):
        y = np.asarray(x[:, c], dtype=float)
        dc = float(np.mean(y))
        y = y - dc
        nper = 2 ** 17 if fs >= 150000 else 2 ** 16
        nper = min(nper, len(y) // 2 * 2)
        f, p = sg.welch(y, fs=fs, window="blackmanharris", nperseg=nper, noverlap=nper // 2,
                        scaling="density", detrend=False)
        df = f[1] - f[0]
        bands = {}
        for name, lo, hi in _bands_silence(fs):
            m = (f >= lo) & (f <= hi)
            bands[name] = float(dsp.rms_dbfs(math.sqrt(float(np.sum(p[m])) * df)))
        m = (f >= 20) & (f <= 20000)
        pa = p[m] * 10 ** (dsp.a_weight_db(f[m]) / 10)
        a_w = float(dsp.rms_dbfs(math.sqrt(float(np.sum(pa)) * df)))
        hum = []
        for k in (1, 2, 3):
            fm = mains * k
            sel = (f > fm - 3 * df) & (f < fm + 3 * df)
            hum.append(float(dsp.rms_dbfs(math.sqrt(float(np.sum(p[sel])) * df))))
        blk = dsp.Block(y, fs)
        res[ch] = {"bands": bands, "a_weighted": a_w, "hum": hum, "dc": float(dsp.db20(abs(dc))),
                   "peak": float(dsp.db20(np.max(np.abs(y)))), "lines": find_lines(blk)}
    return res


def residual_profile(x, fs, f0, n, nharm=9):
    """Негармонический остаток по полосам и его главные линии (усреднение мощностей блоков)."""
    M = len(x) // n
    blocks = np.asarray(x[:M * n], dtype=float).reshape(M, n)
    P = np.mean([dsp.Block(b, fs).P for b in blocks], axis=0)
    ref = dsp.Block(blocks[0], fs)
    df, freq = ref.df, ref.freq
    mask = np.zeros(P.size, dtype=bool)
    mask[:max(1, int(20.0 / df))] = True
    for h in range(1, nharm + 1):
        if h * f0 < fs / 2:
            k = int(round(h * f0 / df))
            mask[max(0, k - 5): k + 6] = True
    R = np.where(mask, 0.0, P)
    bands = {}
    for name, lo, hi in _bands_residual(fs):
        m = (freq >= lo) & (freq < hi)
        s = float(np.sum(R[m]))
        bands[name] = float(dsp.rms_dbfs(math.sqrt(s))) if s > 0 else -300.0
    blk = dsp.Block(blocks[0], fs)
    blk.P = P
    lines = find_lines(blk, exclude=mask, thresh_db=12.0, top=6)
    for ln in lines:
        ln["offset"] = ln["f"] - f0
    return bands, lines


def _med(v):
    return float(np.median(v))


# ------------------------------------------------------------------------ съёмка


def run_rate(rig, levels=(-1.0, -12.0), repeats=3, silence_s=8.0, nharm=9, progress=print):
    """Паспорт на одной частоте дискретизации; rig уже открыт.

    Каждый повтор — ОТДЕЛЬНОЕ открытие карты (rig.run): наводки от преобразователя питания
    зависят от момента запуска потоков, и разброс между открытиями — часть паспорта."""
    fs = rig.fs
    be = rig.be
    io_config = {
        "backend": getattr(be, "name", type(be).__name__),
        "device": getattr(be, "hw", getattr(be, "name", "?")),
        "capture_format": getattr(be, "fmt_in", None),
        "playback_format": getattr(be, "fmt_out", None),
        "capture_channels": getattr(be, "ch_in", None),
        "playback_channels": getattr(be, "ch_out", None),
    }
    out = {"fs": fs, "blocks": rig.blocks, "io_config": io_config, "warnings": []}
    progress(f"  [{fs} Гц] тишина {silence_s:.0f} с…")
    x, notes = capture_silence(rig, silence_s)
    out["warnings"] += notes
    out["silence"] = analyze_silence(x, fs, rig.cal["mains"])

    k, _ = rig.amp_tone(TONE_HZ, 0.0)
    f0 = rig.f_of(k)
    per = {}
    for r in range(int(repeats)):
        progress(f"  [{fs} Гц] тон {f0:.0f} Гц, повтор {r + 1}/{int(repeats)}…")
        segs = []
        for lv in levels:
            kk, a = rig.amp_tone(TONE_HZ, float(lv))
            segs.append(([(kk, a)], [(kk, a)], None))
        wins, warns = rig.run(segs)
        out["warnings"] += warns
        for lv, w in zip(levels, wins):
            for c, ch in ((0, "L"), (1, "R")):
                an = rig._tone_an(w, c, k, nharm)
                hm = {int(h["h"]): h for h in an["harmonics"]}
                bands, lines = residual_profile(w[:, c], fs, f0, rig.n, nharm)
                per.setdefault((ch, float(lv)), []).append({
                    "fund": float(an["level_dbfs"]),
                    "h2": float(an["level_dbfs"] + hm[2]["raw_dbc"]) if 2 in hm else None,
                    "h3": float(an["level_dbfs"] + hm[3]["raw_dbc"]) if 3 in hm else None,
                    "resid": float(an["noise_dbfs"]), "thdn": float(an["level_dbfs"] + an["thdn_db"]),
                    "bands": bands, "lines": lines})
    tone = {"L": {}, "R": {}}
    for (ch, lv), reps in per.items():
        res = [q["resid"] for q in reps]
        worst = max(reps, key=lambda q: q["resid"])
        h2 = [q["h2"] for q in reps if q["h2"] is not None]
        h3 = [q["h3"] for q in reps if q["h3"] is not None]
        tone[ch][f"{lv:g}"] = {
            "n": len(reps), "fund": _med([q["fund"] for q in reps]),
            "h2": _med(h2) if h2 else None, "h3": _med(h3) if h3 else None,
            "resid": _med(res), "resid_min": float(min(res)), "resid_max": float(max(res)),
            "spread": float(max(res) - min(res)), "thdn": _med([q["thdn"] for q in reps]),
            "bands": worst["bands"], "lines": worst["lines"]}
    out["tone"] = tone

    progress(f"  [{fs} Гц] проникновение…")
    kk, a = rig.amp_tone(TONE_HZ, float(levels[0]))
    wins, warns = rig.run([([(kk, a)], [], None), ([], [(kk, a)], None)])
    out["warnings"] += warns
    xt = {}
    for mode, w in zip(("L", "R"), wins):
        drv, oth = (0, 1) if mode == "L" else (1, 0)
        a_d = rig._tone_an(w, drv, k, 2)["level_dbfs"]
        a_o = rig._tone_an(w, oth, k, 2)["level_dbfs"]
        xt[f"{mode}->{'R' if mode == 'L' else 'L'}"] = float(a_o - a_d)
    out["xtalk"] = xt
    return out


def run_passport(make_rig, rates=(96000, 192000), label="", levels=(-1.0, -12.0), repeats=3,
                 silence_s=8.0, progress=print, metadata=None):
    """make_rig(fs) -> открытый Rig. Возвращает dict паспорта.

    metadata stores the full measurement configuration so an A/B comparison
    can refuse silently incomparable runs.
    """
    p = {"schema": SCHEMA, "label": label, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
         "tone_hz": TONE_HZ, "levels_dbfs": [float(v) for v in levels],
         "repeats": int(repeats), "silence_seconds": float(silence_s),
         "rates": {}, "metadata": dict(metadata or {})}
    for fs in rates:
        rig = make_rig(int(fs))
        p["device"] = getattr(rig.be, "hw", getattr(rig.be, "name", "?"))
        p["rates"][str(int(fs))] = run_rate(rig, levels, repeats, silence_s, progress=progress)
    return p


def save(p, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=1)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------- отчёт


def text_passport(p):
    L = [f"ПАСПОРТ ТРАКТА · {p.get('label') or '—'} · {p['time']} · {p.get('device', '?')} · тон {p['tone_hz']:.0f} Гц"]
    for fs, d in p["rates"].items():
        L.append(f"\n=== {int(fs) / 1000:g} кГц ===")
        L.append("Тишина, dBFS:  канал  " + "  ".join(f"{k:>9s}" for k in d["silence"]["L"]["bands"]) +
                 "   A-вес   50 Гц     DC")
        for ch in ("L", "R"):
            s = d["silence"][ch]
            L.append(f"               {ch:>5s}  " + "  ".join(f"{v:9.2f}" for v in s["bands"].values()) +
                     f"  {s['a_weighted']:6.2f}  {s['hum'][0]:6.1f}  {s['dc']:6.1f}")
        for ch in ("L", "R"):
            ln = d["silence"][ch]["lines"]
            if ln:
                L.append(f"  линии в тишине {ch}: " + " · ".join(f"{x['f']:.1f} Гц {x['dbfs']:.1f}" for x in ln[:5]))
        for lv in sorted(d["tone"]["L"], key=lambda s: -float(s)):
            L.append(f"\nТон {lv} dBFS (повторов {d['tone']['L'][lv]['n']}), dBFS:")
            L.append("  канал   тон     H2      H3    остаток med [min…max]   разброс   превышение над тишиной")
            for ch in ("L", "R"):
                t = d["tone"][ch][lv]
                floor = d["silence"][ch]["bands"]["20–20k"]
                h2 = f"{t['h2']:7.1f}" if t["h2"] is not None else "      ·"
                h3 = f"{t['h3']:7.1f}" if t["h3"] is not None else "      ·"
                L.append(f"  {ch:>5s} {t['fund']:6.2f} {h2} {h3}   {t['resid']:7.2f} [{t['resid_min']:.1f}…{t['resid_max']:.1f}]"
                         f"   {t['spread']:5.1f} дБ   {t['resid'] - floor:+6.1f} дБ")
            for ch in ("L", "R"):
                t = d["tone"][ch][lv]
                L.append(f"    остаток {ch} по полосам: " + "  ".join(f"{k} {v:.1f}" for k, v in t["bands"].items()))
                if t["lines"]:
                    L.append(f"    линии остатка {ch}: " + " · ".join(
                        f"{x['f']:.1f} Гц (от тона {x['offset']:+.1f}) {x['dbfs']:.1f}" for x in t["lines"][:4]))
        L.append("\nПроникновение: " + "  ".join(f"{k} {v:.1f} дБ" for k, v in d["xtalk"].items()))
        L += [f"! {w}" for w in d["warnings"]]
    return "\n".join(L)


# ----------------------------------------------------------------- сравнение


def _verdict(delta, tol):
    if delta < -tol:
        return "ЛУЧШЕ", 1
    if delta > tol:
        return "ХУЖЕ", -1
    return "≈ в пределах разброса", 0


def passport_mismatches(a, b):
    """Differences that make a strict before/after comparison unsafe."""
    mismatches = []
    if a.get("device") and b.get("device") and a.get("device") != b.get("device"):
        mismatches.append(f"устройства различаются: {a.get('device')} → {b.get('device')}")
    keys = ("tone_hz", "levels_dbfs", "repeats", "silence_seconds")
    for key in keys:
        va, vb = a.get(key), b.get(key)
        if va is not None and vb is not None and va != vb:
            mismatches.append(f"{key}: {va!r} → {vb!r}")
    ma, mb = a.get("metadata", {}), b.get("metadata", {})
    for key in ("in_map", "out_map", "format", "blocks"):
        va, vb = ma.get(key), mb.get(key)
        if va is not None and vb is not None and va != vb:
            mismatches.append(f"{key}: {va!r} → {vb!r}")
    for rate in sorted(set(a.get("rates", {})) & set(b.get("rates", {})), key=int):
        io_a = a["rates"][rate].get("io_config")
        io_b = b["rates"][rate].get("io_config")
        if io_a and io_b and io_a != io_b:
            mismatches.append(f"PCM-конфигурация на {int(rate) / 1000:g} кГц различается: {io_a} → {io_b}")
    return mismatches


def compare_passports(a, b, allow_mismatch=False):
    """Return text + row stats; refuse unlike devices/configs unless explicitly allowed."""
    mismatches = passport_mismatches(a, b)
    common_rates = set(a.get("rates", {})) & set(b.get("rates", {}))
    if mismatches and not allow_mismatch:
        raise ValueError("Паспорта нельзя строго сравнить: " + "; ".join(mismatches)
                         + ". Для исследовательского сравнения разных карт укажи --allow-device-mismatch.")
    shared_rates = sorted(set(a.get("rates", {})) & set(b.get("rates", {})), key=int)
    if not shared_rates:
        raise ValueError("У паспортов нет общих частот дискретизации")
    rows, L = [], []
    L.append(f"СРАВНЕНИЕ: «{a.get('label') or 'до'}» ({a['time']}) → «{b.get('label') or 'после'}» ({b['time']})")
    if mismatches:
        L.append("ПРЕДУПРЕЖДЕНИЕ: это не строгое A/B-сравнение: " + "; ".join(mismatches))
    elif not a.get("metadata") or not b.get("metadata"):
        L.append("ПРЕДУПРЕЖДЕНИЕ: старый паспорт не содержит полной конфигурации; проверь физическую распайку и настройки вручную.")
    missing_io = [fs for fs in shared_rates if bool(a["rates"][fs].get("io_config")) != bool(b["rates"][fs].get("io_config"))]
    if missing_io:
        L.append("ПРЕДУПРЕЖДЕНИЕ: в одном из паспортов нет фактических ALSA PCM-форматов для "
                 + ", ".join(f"{int(fs)/1000:g} кГц" for fs in missing_io)
                 + "; совместимость форматов проверить вручную.")
    for fs in shared_rates:
        da, db_ = a["rates"][fs], b["rates"][fs]
        L.append(f"\n=== {int(fs) / 1000:g} кГц ===")
        L.append(f"{'показатель':46s} {'до':>9s} {'после':>9s} {'Δ':>7s}   вердикт")

        def add(name, va, vb, tol):
            if va is None or vb is None:
                return
            d = vb - va
            word, sign = _verdict(d, tol)
            rows.append({"rate": int(fs), "name": name, "before": va, "after": vb, "delta": d, "sign": sign})
            L.append(f"{name:46s} {va:9.2f} {vb:9.2f} {d:+7.2f}   {word}")

        for ch in ("L", "R"):
            sa, sb = da["silence"][ch], db_["silence"][ch]
            for bn in sa["bands"]:
                if bn in sb["bands"]:
                    add(f"{ch}: шум тишины {bn}, dBFS", sa["bands"][bn], sb["bands"][bn], 0.5)
            add(f"{ch}: фон 50 Гц, dBFS", sa["hum"][0], sb["hum"][0], 3.0)
            for lv in sorted(set(da["tone"][ch]) & set(db_["tone"][ch]), key=lambda s: -float(s)):
                ta, tb = da["tone"][ch][lv], db_["tone"][ch][lv]
                tol = max(1.0, ta["spread"], tb["spread"])
                add(f"{ch}: остаток с тоном {lv} dBFS (медиана)", ta["resid"], tb["resid"], tol)
                add(f"{ch}: разброс остатка {lv} dBFS", ta["spread"], tb["spread"], 1.5)
                if ta["h3"] is not None and tb["h3"] is not None:
                    add(f"{ch}: H3 {lv} dBFS (абс.)", ta["h3"], tb["h3"], 1.5)
                if ta["h2"] is not None and tb["h2"] is not None:
                    add(f"{ch}: H2 {lv} dBFS (абс.)", ta["h2"], tb["h2"], 1.5)
        for key in sorted(set(da["xtalk"]) & set(db_["xtalk"])):
            add(f"проникновение {key}, дБ", da["xtalk"][key], db_["xtalk"][key], 2.0)
        top = sorted(set(da["tone"]["L"]), key=lambda s: -float(s))[0]
        for tag, d in (("до", da), ("после", db_)):
            tl, tr = d["tone"]["L"][top], d["tone"]["R"][top]
            ex_l = tl["resid"] - d["silence"]["L"]["bands"]["20–20k"]
            ex_r = tr["resid"] - d["silence"]["R"]["bands"]["20–20k"]
            L.append(f"  индекс аномалии ({tag}): превышение остатка над тишиной при {top} dBFS: "
                     f"L {ex_l:+.1f} дБ, R {ex_r:+.1f} дБ, асимметрия L−R {tl['resid'] - tr['resid']:+.1f} дБ")
    better = sum(1 for r in rows if r["sign"] > 0)
    worse = sum(1 for r in rows if r["sign"] < 0)
    same = len(rows) - better - worse
    key = [r for r in rows if "остаток с тоном" in r["name"]]
    L.append("")
    if key:
        kb = max(key, key=lambda r: abs(r["delta"]))
        L.append(f"ГЛАВНОЕ: {kb['name']} ({int(kb['rate']) / 1000:g} кГц): {kb['before']:.1f} → {kb['after']:.1f} dBFS, "
                 f"{'ЛУЧШЕ' if kb['sign'] > 0 else 'ХУЖЕ' if kb['sign'] < 0 else 'без изменения'} на {abs(kb['delta']):.1f} дБ")
    L.append(f"ИТОГ: лучше {better}, хуже {worse}, без изменений {same}. "
             + ("Общий вердикт: ЛУЧШЕ." if better > worse and worse == 0 else
                "Общий вердикт: ХУЖЕ." if worse > better and better == 0 else
                "Общий вердикт: смешанный — смотри строки выше." if better and worse else
                "Общий вердикт: заметных изменений нет."))
    L.append("Примечание: счётчик показателей — не единый индекс качества; оценивай прежде всего остаток, разброс, THD и проникновение отдельно.")
    return "\n".join(L), {"better": better, "worse": worse, "same": same, "rows": rows}


# ---------------------------------------------------------------- живой монитор


def watch_loop(rig, level=-1.0, f=TONE_HZ, seconds=3, count=None, baseline=None, out=print, sleep=time.sleep):
    """Каждые ~1.5 с печатает негармонический остаток L и R при непрерывном тоне на обоих каналах.
    Для работы паяльником/пинцетом: сразу видно, что делает прикосновение или правка."""
    fs = rig.fs
    be = rig.be
    k, a = rig.amp_tone(f, level)
    f0 = rig.f_of(k)
    t = np.arange(rig.n, dtype=np.int64)
    s = a * np.sin(2 * np.pi * ((k * t) % rig.n) / rig.n)
    be.live_start()
    try:
        be.live_set_loop(np.column_stack((s, s)))
        sleep(2.0 + seconds)
        hist = []
        i = 0
        while count is None or i < count:
            x = be.live_recent(seconds + 0.2)
            if len(x) >= seconds * fs:
                x = x[-int(seconds * fs):]
                res = []
                for c in (0, 1):
                    r = dsp.analyze_tone(x[:, c], fs, f0, rig.n, band=(20.0, 20000.0), mains=rig.cal["mains"])
                    res.append((float(r["noise_dbfs"]), float(r["level_dbfs"])))
                hist.append(res)
                if baseline is None and len(hist) == 3:
                    baseline = (_med([h[0][0] for h in hist]), _med([h[1][0] for h in hist]))
                note = ""
                if baseline is not None:
                    dl, dr = res[0][0] - baseline[0], res[1][0] - baseline[1]
                    mark = lambda d: "▲хуже" if d > 1.5 else "▼лучше" if d < -1.5 else "≈"
                    note = f"  | к эталону: L {dl:+5.1f} дБ {mark(dl)}  R {dr:+5.1f} дБ {mark(dr)}"
                out(f"{time.strftime('%H:%M:%S')}  остаток L {res[0][0]:7.2f}  R {res[1][0]:7.2f} dBFS  "
                    f"(тон {res[0][1]:.2f} / {res[1][1]:.2f}){note}")
                i += 1
            sleep(max(0.5, seconds / 2))
    finally:
        be.live_set_loop(None)
        be.live_stop()
    return hist


# ------------------------------------------------------- сверка живого PSD с цифрами


def psd_check(rig, seconds=8.0, out=print):
    """Сравнивает плотность шума: формула живого спектра (один кадр и сглаживание) против Welch."""
    fs = rig.fs
    x, _ = capture_silence(rig, seconds)
    res = {}
    n = 262144
    lo, hi = 1000.0, 10000.0
    for c, ch in ((0, "L"), (1, "R")):
        y = x[:, c] - np.mean(x[:, c])
        nper = min(131072, len(y) // 2 * 2)
        f, p = sg.welch(y, fs=fs, window="blackmanharris", nperseg=nper, noverlap=nper // 2, scaling="density", detrend=False)
        m = (f >= lo) & (f <= hi)
        welch_med = float(np.median(10 * np.log10(2 * p[m])))
        welch_mean = float(10 * np.log10(2 * np.mean(p[m])))
        frames = [dsp.Block(y[i:i + n], fs) for i in range(0, len(y) - n + 1, n // 2)] if len(y) >= n else []
        live = None
        if frames:
            b0 = frames[0]
            msk = (b0.freq >= lo) & (b0.freq <= hi)
            single = float(np.median(dsp.spectrum_asd_dbfs(frames[-1])[msk]))
            pw = np.mean([b.P for b in frames], axis=0)
            live = {"single_frame_median": single,
                    "mean_power_of_frames": float(10 * np.log10(2 * np.mean(pw[msk]) / b0.df))}
        tot = float(dsp.rms_dbfs(math.sqrt(float(np.sum(p[(f >= 20) & (f <= 20000)])) * (f[1] - f[0]))))
        res[ch] = {"welch_median": welch_med, "welch_mean": welch_mean, "live": live, "total_20_20k": tot}
        out(f"[{ch}] интеграл 20–20k {tot:.2f} dBFS → ожидаемая плотность {tot - 10 * math.log10(20000):.1f} dBFS/√Гц")
        out(f"     Welch 1–10 кГц: среднее {welch_mean:.1f}, медиана {welch_med:.1f} dBFS/√Гц")
        if live:
            out(f"     формула живого спектра (FFT {n}): один кадр, медиана {live['single_frame_median']:.1f}; "
                f"среднее по кадрам {live['mean_power_of_frames']:.1f} dBFS/√Гц")
        else:
            out("     для FFT 256k нужна запись длиннее 1.4 с")
    return res
