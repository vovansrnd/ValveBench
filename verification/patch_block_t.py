#!/usr/bin/env python3
"""Добавляет блок T (адресные исправления по ревью координатора) в регрессионный набор."""
import io

P = "/opt/MyApps/Adudio/for_z/ValveBench-0.1.19/verification/test_gainmatch_0119.py"
src = io.open(P, encoding="utf-8").read()

block_t = '''
# ============================================================ БЛОК T: ревью координатора

def block_t():
    print("\\n=== Блок T: адресные исправления по ревью координатора (0.1.19-R2) ===")
    fs = 48000
    freqs = (1000.0, 1500.0)
    t = np.arange(int(0.6 * fs)) / fs
    tone = amp_of(-20.0) * np.sin(2 * math.pi * 1000.0 * t)

    # T1: Live — лампа с сильными гармониками валидна (C/R, а не SINAD)
    h2 = amp_of(-32.0) * np.sin(2 * math.pi * 2000.0 * t + 0.7)
    h3 = amp_of(-40.0) * np.sin(2 * math.pi * 3000.0 * t + 1.9)
    rng = np.random.default_rng(41)
    nz = band_limited_noise(t.size, fs, "white", -110.0, rng)
    ring_lamp = np.column_stack([tone + h2 + h3 + nz, tone + h2 + h3 + nz])
    tr = gainmatch.GainTracker()
    st = tr.update(ring_lamp, fs, 1000.0, -20.0, 0, None, now=0.0)
    check("T1", "Live: лампа H2=−32/H3=−40 дБс (SINAD≈31) — валидна, C/R высокий",
          "ok", st["valid"] and min(st["carrier_residual_db"]) > 60.0,
          "0.1.19-R2: гармоники вычтены базисом → C/R высокий; SINAD — диагностика",
          f"valid={st['valid']}; C/R={['%.1f' % v for v in st['carrier_residual_db']]}; "
          f"SINAD={['%.1f' % v for v in st['sinad_db']]}")

    # T2: Live — шум (C/R < 30) бракуется с понятным сообщением
    nz_loud = band_limited_noise(t.size, fs, "white", -40.0, rng)
    ring_noisy = np.column_stack([tone + nz_loud, tone + nz_loud])
    st_n = tr.update(ring_noisy, fs, 1000.0, -20.0, 0, None, now=1.0)
    check("T2", "Live: шум (C/R≈4 дБ) бракуется воротами C/R",
          "ok", (not st_n["valid"]) and "C/R" in st_n["msg"],
          "ожидание: invalid, причина C/R",
          f"valid={st_n['valid']}; msg={st_n['msg'][:70]}")

    # T3: Live — щелчок внутри кольца бракуется подоконным контролем
    ring_b = ring_lamp.copy()
    n_b = int(0.05 * fs)
    tt = np.arange(n_b) / fs
    ring_b[int(0.2 * fs):int(0.2 * fs) + n_b, 0] += amp_of(-37.0) * np.sin(2 * math.pi * 1000.0 * tt)
    st_b = tr.update(ring_b, fs, 1000.0, -20.0, 0, None, now=2.0)
    check("T3", "Live: щелчок внутри окна — unstable (подоконный контроль в Live)",
          "ok", (not st_b["valid"]) and "нестабильна" in st_b["msg"],
          "ожидание: invalid с сообщением о нестабильности несущей",
          f"valid={st_b['valid']}; msg={st_b['msg'][:70]}")

    # T4: Live — стабильный сигнал на границе C/R≈30 не бракуется подокнами
    # (5 подокон по 100 мс достаточно, т.к. контроль выполняется после C/R-ворот)
    fp_ok = True
    worst = 0.0
    for seed in range(8):
        rng_i = np.random.default_rng(100 + seed)
        nz_i = band_limited_noise(t.size, fs, "white", -52.0, rng_i)   # C/R≈30
        r_i = np.column_stack([tone + nz_i, tone + nz_i])
        st_i = tr.update(r_i, fs, 1000.0, -20.0, 0, None, now=3.0 + 0.1 * seed)
        if st_i["valid"]:
            worst = max(worst, max(s["trend_span"] for s in st_i["envelope"]))
        fp_ok &= st_i["valid"]
    check("T4", "Live: 8 реализаций C/R≈30 — ни одного ложного брака подокнами",
          "ok", fp_ok,
          "ожидание: при C/R≥30 флуктуации 5 подокон (100 мс) ниже порога 0.005",
          f"все валидны: {fp_ok}; макс. trend_span={worst:.5f}")

    # T5: контекстный TARE — отсутствующий ключ = несовместимость
    tr2 = gainmatch.GainTracker()
    ctx_full = {"freq": 1000.0, "level_dbfs": -20.0, "fs": fs, "route": 0,
                "device_key": "sim", "mixer_hash": "h", "cal_sig": "c"}
    ring_t = np.column_stack([0.1 * np.sin(2 * math.pi * 1000 * np.arange(fs) / fs)] * 2)
    tr2.update(ring_t, fs, 1000.0, -20.0, 0, None, now=0.0, ctx=ctx_full)
    tr2.tare(ctx=ctx_full)
    ctx_no_freq = {k: v for k, v in ctx_full.items() if k != "freq"}
    st5 = tr2.update(ring_t, fs, 1000.0, -20.0, 0, None, now=1.0, ctx=ctx_no_freq)
    check("T5", "TARE: отсутствующий обязательный ключ контекста = безопасный сброс",
          "ok", st5["tare_db"] == 0.0 and st5.get("tare_reset_reason") is not None,
          f"reason={st5.get('tare_reset_reason')}")

    # T6: RIAA — подсказка из сообщения об ошибке приводит к успешному повтору
    be_r = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_r = make_rig(be_r, fs)
    hint_base = None
    try:
        run_series(rig_r, be_r, (100.0, 1000.0, 10000.0), level_dbfs=-12.0,
                   repeats=1, riaa_compensate=True)
    except Exception as exc:
        m = re.search(r"до ([+-][0-9.]+) dBFS", str(exc))
        hint_base = float(m.group(1)) if m else None
    retry_ok = False
    if hint_base is not None:
        res_h = run_series(rig_r, be_r, (100.0, 1000.0, 10000.0),
                           level_dbfs=hint_base, repeats=1, riaa_compensate=True)
        retry_ok = all(row["valid"] for row in res_h["rows"])
    check("T6", "RIAА: подсказка об отказе рабочая — повтор с предложенной базой успешен",
          "ok", hint_base is not None and retry_ok,
          f"подсказка: база {hint_base} dBFS → повтор без отказа, valid=True")

    # T7: C/R не считает H4/H5/H7 шумом (nharm=9)
    be_h = ScriptedBackend(fs=fs, noise_dbfs=-110.0,
                           extra_lines=(((4000.0, -64.0, 0.7), (5000.0, -70.0, 1.3),
                                         (7000.0, -74.0, 2.2)), ()))
    rig_h = make_rig(be_h, fs)
    res_h47 = run_series(rig_h, be_h, freqs, repeats=1)
    cr = res_h47["rows"][0]["channels"]["L"]["carrier_residual_db"]
    check("T7", "C/R: H4/H5/H7 (−40…−50 дБс) не превращаются в «шум» — ворота чисты",
          "ok", res_h47["rows"][0]["valid"] and cr > 60.0,
          "ожидание: nharm=9 исключает H2…H9 из негармонического остатка",
          f"valid={res_h47['rows'][0]['valid']}; C/R={cr:.1f} дБ")

    # T8: proximity — 9994 Гц против 200-й гармоники (сеть 49.97 Гц)
    be_p = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_p = make_rig(be_p, fs)
    rig_p.cal["mains"] = 49.97
    res_p = run_series(rig_p, be_p, (94.0, 994.0, 9994.0), repeats=1)
    prox9994 = any("9994" in str(w) for w in res_p["warnings"])
    check("T8", "proximity проверяет гармоники до частоты ряда: 9994 Гц vs 200-я "
                "(200×49.97=9994) — предупреждение выдано",
          "ok", prox9994 and all(row["valid"] for row in res_p["rows"]),
          f"warnings={[w[:60] for w in res_p['warnings'][:2]]}")

    # T9: щелчок на частоте тона в лид-тишине — onset на тоне
    be_o = ScriptedBackend(fs=fs, noise_dbfs=-125.0, burst=(0.05, 0.05, -6.0, (0, 1)))
    rig_o = make_rig(be_o, fs)
    res_o = run_series(rig_o, be_o, freqs, repeats=1)
    check("T9", "Щелчок на частоте тона в лид-тишине: onset на тоне, серия валидна",
          "ok", all(row["valid"] for row in res_o["rows"]),
          "узкополосный поиск с критерием устойчивости игнорирует короткий щелчок",
          f"valid={[row['valid'] for row in res_o['rows']]}")

    # T10: CSV — заголовки SINAD
    import tempfile
    from valvebench import store as store_mod
    with tempfile.TemporaryDirectory(prefix="gm-0119-") as tmp:
        store_mod.save_gain_series(res_p, tmp)
        csv_path = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".csv")][0]
        header = open(csv_path, encoding="utf-8").readline()
    check("T10", "CSV: заголовки SINAD_L_db/SINAD_R_db (не SNR)",
          "ok", "SINAD_L_db" in header and "SINAD_R_db" in header and "SNR_L_db" not in header,
          f"заголовок: {header[:110]}")


'''

anchor = "# ============================================================ main"
assert anchor in src
src = src.replace(anchor, block_t + "\n" + anchor)
src = src.replace(
    """    block_r()
    block_s()
    n_all = len(RESULTS)""",
    """    block_r()
    block_s()
    block_t()
    n_all = len(RESULTS)""")
io.open(P, "w", encoding="utf-8").write(src)
print("block T added")
