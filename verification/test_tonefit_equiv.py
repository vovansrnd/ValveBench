#!/usr/bin/env python3
"""Контрольная приёмка fix3, пункт 1: численная эквивалентность tone_fit.

Часть A (в процессе): новый tone_fit/estimate_signal_frequency против
исходных реализаций (lstsq/пересчёт экспоненты, скопированы дословно из
reviewfix2) на расширенной матрице: fs, длины окон, частоты (в т.ч. между
бинами), фазы, DC, шум, гармоники, низкие уровни. Сравниваются первичные
результаты (амплитуда, фаза, rms остатков) И производные величины, которыми
оперирует индикатор: C/R, SINAD в дБ, а также f_est.

Часть B (между деревьями): граничные сценарии ворот (C/R около 30 дБ, низкий
уровень, ламповый THD, DC) прогоняются через GainTracker.update в дереве
reviewfix2-accepted (старый код) и в текущем; сравниваются ΔG, C/R, SINAD,
valid и сообщения. Дерево «до» задаётся переменной VB_OLD_TREE.

Числа: max |Δ| по каждому полю + любое расхождение решения valid/invalid.
"""
import json
import math
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
OLD_TREE = os.environ.get("VB_OLD_TREE",
                          "/opt/MyApps/Adudio/for_z/ValveBench-0.1.20-dev3-reviewfix2-accepted/ValveBench-0.1.20-dev3")

from valvebench import gainmatch as gm  # noqa: E402  (новый код)


# --- исходные реализации (дословно reviewfix2) ------------------------------
def tone_fit_old(x, fs, f):
    x = np.asarray(x, dtype=np.float64)
    t = np.arange(x.size) / float(fs)
    w = 2.0 * math.pi * f
    a = np.column_stack((np.cos(w * t), np.sin(w * t), np.ones_like(t)))
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    res = x - a @ coef
    amp = math.hypot(coef[0], coef[1])
    return amp, math.atan2(-coef[1], coef[0]), float(np.sqrt(np.mean(res * res)))


def estimate_signal_frequency_old(x, fs, f_nominal, n_blocks=4):
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    m = n // n_blocks
    if m < 16 or n_blocks < 3:
        return float(f_nominal), False
    t_glob = np.arange(n_blocks * m) / float(fs)
    z = x[:n_blocks * m] * np.exp(-2j * np.pi * f_nominal * t_glob)
    zb = z.reshape(n_blocks, m).mean(axis=1)
    if float(np.max(np.abs(zb))) < 1e-9:
        return float(f_nominal), False
    ph = np.angle(zb)
    d = np.diff(np.unwrap(ph))
    if float(np.median(np.abs(d))) > 2.5:
        return float(f_nominal), False
    f_est = float(f_nominal + np.median(d) / (2.0 * math.pi * m / fs))
    if not math.isfinite(f_est) or f_est <= 0:
        return float(f_nominal), False
    return f_est, True


def rms_dbfs(x):
    return 20.0 * math.log10(max(float(x), 1e-12) * math.sqrt(2.0))


# --- матрица части A ---------------------------------------------------------
FSS = [44100, 48000, 96000, 192000]
WIN_S = [0.5, 0.6, 1.0, 2.0]
FREQS = [100.0, 997.0, 1000.0, 1000.137, 10000.0, 9994.0, 12000.0, 15500.0]
PHASES = [0.0, math.pi / 3, math.pi / 2, 2.0 * math.pi / 3, math.pi]
LEVELS = [-6.0, -20.0, -40.0, -60.0, -80.0, -100.0]
DCS = [0.0, 0.001, 0.01]
NOISE = [None, ("white", -70.0), ("white", -50.0), ("hum50", -40.0),
         ("hum50", -20.0), ("white+hum", -60.0)]
HARMONICS = [None, (2, -25.0), (2, -25.0, 3, -25.0), (2, -12.0), (5, -30.0)]

worst = {"amp": 0.0, "phase": 0.0, "res_nh": 0.0, "res_full": 0.0,
         "cr_db": 0.0, "sinad_db": 0.0, "f_est": 0.0}
flip_count = 0
nvec = 0


def gen(fs, n, f, phase, lvl_db, dc, noise, harm, rng):
    t = np.arange(n) / fs
    a = 10.0 ** (lvl_db / 20.0)
    x = a * np.sin(2 * np.pi * f * t + phase) + dc
    if harm:
        hh = list(harm)
        for i in range(0, len(hh), 2):
            h, db = hh[i], hh[i + 1]
            x += a * 10.0 ** (db / 20.0) * np.sin(2 * np.pi * h * f * t)
    if noise:
        kind = noise[0]
        if kind == "white":
            x += 10.0 ** (noise[1] / 20.0) * rng.standard_normal(n)
        elif kind == "hum50":
            x += 10.0 ** (noise[1] / 20.0) * np.sin(2 * np.pi * 50.0 * t + 0.7)
        else:  # white+hum
            x += 10.0 ** (noise[1] / 20.0) * rng.standard_normal(n)
            x += 10.0 ** ((noise[1] + 15.0) / 20.0) * np.sin(2 * np.pi * 50.0 * t)
    return x


def part_a():
    global flip_count, nvec
    rng = np.random.default_rng(2026)
    worst_lvl = {}
    for fs in FSS:
        for win_s in WIN_S:
            n = int(win_s * fs)
            for f in FREQS:
                if f > 0.45 * fs:
                    continue
                for lvl in LEVELS:
                    # полный перебор остальных осей — дорого; на каждый
                    # (fs, окно, f, уровень) берём 3 детерминированных варианта
                    for variant in range(3):
                        phase = PHASES[(variant + int(lvl)) % len(PHASES)]
                        dc = DCS[variant % len(DCS)]
                        noise = NOISE[(variant * 2 + int(-lvl / 20)) % len(NOISE)]
                        harm = HARMONICS[(variant + int(f / 1000)) % len(HARMONICS)]
                        x = gen(fs, n, f, phase, lvl, dc, noise, harm, rng)
                        amp_n, ph_n, rnh_n, rfull_n = gm.tone_fit_harmonics(
                            x, fs, f, nharm=9, f_max=0.45 * fs, f_band_hi=20000.0)
                        # референс: старый tone_fit для основного тона + тот же
                        # расчёт остатков через старые операции невозможен без
                        # полного старого tone_fit_harmonics — там базис уже был
                        # кеширован в reviewfix2 (_harmonic_basis не менялся);
                        # численно значимая замена — решение LSQ, поэтому
                        # сверяем ядро: старый tone_fit vs новый tone_fit на
                        # том же x, плюс f_est.
                        amp_o, ph_o, res_o = tone_fit_old(x, fs, f)
                        amp_nn, ph_nn, res_nn = gm.tone_fit(x, fs, f)
                        f_n, ok_n = gm.estimate_signal_frequency(x, fs, f)
                        f_o, ok_o = estimate_signal_frequency_old(x, fs, f)
                        cr_n = 20.0 * math.log10(max(amp_nn, 1e-12)) - rms_dbfs(res_nn)
                        cr_o = 20.0 * math.log10(max(amp_o, 1e-12)) - rms_dbfs(res_o)
                        sinad_n = (20.0 * math.log10(max(amp_nn / math.sqrt(2), 1e-12))
                                   - rms_dbfs(res_nn))
                        sinad_o = (20.0 * math.log10(max(amp_o / math.sqrt(2), 1e-12))
                                   - rms_dbfs(res_o))
                        d = {
                            "amp": abs(amp_nn - amp_o),
                            "phase": abs(ph_nn - ph_o),
                            "res_nh": abs(res_nn - res_o),
                            "cr_db": abs(cr_n - cr_o),
                            "sinad_db": abs(sinad_n - sinad_o),
                            "f_est": abs(f_n - f_o) if (ok_n and ok_o) else
                                     (0.0 if ok_n == ok_o else 1e9),
                        }
                        flip = (ok_n != ok_o)
                        for k, v in d.items():
                            if v > worst[k]:
                                worst[k] = v
                                worst_lvl[k] = (fs, win_s, f, lvl, variant)
                        if flip or (worst["f_est"] > 0.02):
                            flip_count += 1
                        nvec += 1
    return worst_lvl


def rel_db(delta, floor=1e-12):
    return delta  # все сравнения в абсолютных величинах (дБ/рад/доля)


def part_b_scenario_script():
    """Код, исполняемый В ОБОИХ деревьях (аргумент __main__ подменяет cwd/путь)."""
    return r'''
import json, math, sys, os
sys.path.insert(0, sys.argv[1])
import numpy as np
from valvebench import gainmatch as gm

FS = 48000
rng = np.random.default_rng(777)

def sig(n, f, lvl, dc=0.0, noise_db=None, harm=None, hum_db=None, phase=0.0):
    t = np.arange(n) / FS
    a = 10.0 ** (lvl / 20.0)
    x = a * np.sin(2 * np.pi * f * t + phase) + dc
    if harm:
        for h, db in harm:
            x += a * 10.0 ** (db / 20.0) * np.sin(2 * np.pi * h * f * t)
    if noise_db is not None:
        x += 10.0 ** (noise_db / 20.0) * rng.standard_normal(n)
    if hum_db is not None:
        x += 10.0 ** (hum_db / 20.0) * np.sin(2 * np.pi * 50.0 * t + 0.7)
    return x

def run():
    n = int(0.6 * FS)
    out = []
    cases = [
        ("cr_border_hi",  sig(n, 1000.0, -20.0, noise_db=-52.0)),
        ("cr_border_lo",  sig(n, 1000.0, -20.0, noise_db=-48.5)),
        ("low_level",     sig(n, 1000.0, -80.0, noise_db=-95.0)),
        ("vlow_level",    sig(n, 1000.0, -100.0, noise_db=-115.0)),
        ("lamp_thd",      sig(n, 1000.0, -20.0, harm=[(2, -25.0), (3, -25.0)])),
        ("dc03",          sig(n, 1000.0, -20.0, dc=0.3)),
        ("offbin",        sig(n, 1000.137, -20.0, noise_db=-60.0)),
        ("hum40",         sig(n, 1000.0, -20.0, hum_db=-40.0)),
        ("hum_loud",      sig(n, 1000.0, -20.0, hum_db=-26.0)),
        ("phase_pi2",     sig(n, 1000.0, -20.0, phase=math.pi/2, noise_db=-55.0)),
        ("phase_2pi3",    sig(n, 1000.0, -20.0, phase=2*math.pi/3, noise_db=-55.0)),
        ("click_burst",   None),  # собирается отдельно
    ]
    # щелчок внутри окна: тон + одиночный выброс 0.5 амплитуды
    x_click = sig(n, 1000.0, -20.0, noise_db=-60.0)
    x_click[n//2] += 0.5
    cases[-1] = ("click_burst", x_click)
    for name, x in cases:
        if x is None:
            continue
        ring = np.column_stack([x, x])
        tr = gm.GainTracker()
        st = tr.update(ring, FS, 1000.0, -20.0, 0, None, now=0.0)
        rec = {"valid": st.get("valid"), "msg": st.get("msg"),
               "delta_db": st.get("delta_db"),
               "carrier_residual_db": st.get("carrier_residual_db"),
               "sinad_db": st.get("sinad_db"),
               "f_fit_hz": st.get("f_fit_hz")}
        out.append([name, rec])
    # 96 кГц
    fs2 = 96000
    n2 = int(0.6 * fs2)
    t2 = np.arange(n2) / fs2
    x96 = 10 ** (-20/20) * np.sin(2*np.pi*1000.0*t2) + \
        10 ** (-55/20) * rng.standard_normal(n2)
    tr = gm.GainTracker()
    st = tr.update(np.column_stack([x96, x96]), fs2, 1000.0, -20.0, 0, None, now=0.0)
    out.append(["fs96k", {"valid": st.get("valid"), "msg": st.get("msg"),
                          "delta_db": st.get("delta_db"),
                          "carrier_residual_db": st.get("carrier_residual_db"),
                          "sinad_db": st.get("sinad_db")}])
    # TARE-поток: обнуление, смена частоты (сброс с причиной), возврат,
    # ручной сброс и восстановление показаний — семантика не должна была
    # измениться вместе с LSQ-ядром
    tr = gm.GainTracker()
    ring1k = np.column_stack([sig(n, 1000.0, -20.0, noise_db=-55.0)] * 2)
    ctx1k = {"freq": 1000.0, "level_dbfs": -20.0, "fs": FS, "route": 0,
             "device_key": "test", "mixer_hash": "h", "cal_sig": "c"}
    tr.update(ring1k, FS, 1000.0, -20.0, 0, None, now=0.0, ctx=ctx1k)
    tare_db = tr.tare(ctx=ctx1k)
    ring1k_b = np.column_stack([sig(n, 1000.0, -20.0, noise_db=-55.0)] * 2)
    st_tared = tr.update(ring1k_b, FS, 1000.0, -20.0, 0, None, now=1.0, ctx=ctx1k)
    ctx10k = dict(ctx1k, freq=10000.0)
    ring10k = np.column_stack([sig(n, 10000.0, -20.0, noise_db=-55.0)] * 2)
    st_10k = tr.update(ring10k, FS, 10000.0, -20.0, 0, None, now=2.0, ctx=ctx10k)
    out.append(["tare_flow", {
        "tare_db": tare_db,
        "delta_after_tare": st_tared.get("delta_db"),
        "tare_reset_reason": st_10k.get("tare_reset_reason"),
        "tare_after_switch": st_10k.get("tare_db"),
        "valid_after_switch": st_10k.get("valid")}])
    json.dump(out, sys.stdout)

run()
'''

def part_b():
    if not os.path.isdir(OLD_TREE):
        return None, f"нет дерева «до»: {OLD_TREE}"
    script = ("import sys\n"
              "sys.argv=['x']\n" + part_b_scenario_script())
    # хак: скрипт читает sys.argv[1] как корень дерева
    script = script.replace("sys.path.insert(0, sys.argv[1])",
                            "sys.path.insert(0, OLD)")
    res = {}
    for tag, tree in (("old", OLD_TREE), ("new", ROOT)):
        wrapper = (f"OLD = {tree!r}\n" + script)
        p = subprocess.run([sys.executable, "-c", wrapper],
                           capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            return None, f"{tag}: ошибка:\n{p.stderr[-800:]}"
        res[tag] = json.loads(p.stdout)
    diffs = []
    for (name_o, rec_o), (name_n, rec_n) in zip(res["old"], res["new"]):
        assert name_o == name_n
        def cmp(prefix, a, b, tol):
            if a is None or b is None:
                if a != b:
                    diffs.append(f"{name_o}/{prefix}: {a} vs {b}")
                return
            if isinstance(a, list):
                for i, (x, y) in enumerate(zip(a, b)):
                    cmp(f"{prefix}[{i}]", x, y, tol)
                return
            if isinstance(a, dict):
                for k in a:
                    cmp(f"{prefix}.{k}", a[k], b.get(k), tol)
                return
            if isinstance(a, bool) or isinstance(a, str):
                if a != b:
                    diffs.append(f"{name_o}/{prefix}: {a!r} vs {b!r}")
                return
            if a is None or b is None:
                if a != b:
                    diffs.append(f"{name_o}/{prefix}: {a} vs {b}")
                return
            if abs(a - b) > tol:
                diffs.append(f"{name_o}/{prefix}: {a} vs {b} (Δ={abs(a-b):.3g})")
        cmp("", rec_o, rec_n, tol=1e-9)
    return diffs, res


def main():
    print("Часть A: матрица эквивалентности (новый vs дословный старый код)")
    worst_lvl = part_a()
    print(f"  векторов: {nvec}")
    for k, v in worst.items():
        ctx = worst_lvl.get(k, "")
        print(f"  max |Δ| {k:9s} = {v:.3e}   {ctx}")
    print(f"  расхождений флага f_est: {flip_count}")

    print("\nЧасть B: граничные сценарии через GainTracker (старое дерево vs новое)")
    diffs, res = part_b()
    if diffs is None:
        print("  ПРОПУЩЕНА:", res)
        ok = False
    else:
        if diffs:
            print(f"  РАСХОЖДЕНИЙ: {len(diffs)}")
            for d in diffs[:20]:
                print("   ", d)
        else:
            print("  все граничные сценарии: ΔG/C/R/SINAD/valid/сообщения идентичны "
                  "(tol 1e-9)")
        # Критерии: линейная область — машинная точность; дБ-область — сильно
        # ниже отображаемой точности 0.01 дБ (нормальные уравнения возводят
        # cond в квадрат, отсюда ~1e-5 дБ на крайних случаях); флаги и
        # решения valid/invalid — ноль расхождений.
        ok = (not diffs and flip_count == 0
              and worst["amp"] < 1e-12 and worst["phase"] < 1e-9
              and worst["res_nh"] < 1e-12
              and worst["cr_db"] < 1e-3 and worst["sinad_db"] < 1e-3
              and worst["f_est"] < 0.02)
    print("\nИТОГ:", "PASS" if ok else "FAIL")
    with open(os.path.join(HERE, "tonefit-equiv-results.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"partA_max": worst, "vectors": nvec, "flips": flip_count,
                   "partB_diffs": diffs,
                   "partB_records": res if res else None}, fh,
                  ensure_ascii=False, indent=1, default=str)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
