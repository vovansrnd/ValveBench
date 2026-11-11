#!/usr/bin/env python3
"""Добавляет блок U (финальная доводка R3 по дополнению координатора)."""
import io

P = "/opt/MyApps/Adudio/for_z/ValveBench-0.1.19/verification/test_gainmatch_0119.py"
src = io.open(P, encoding="utf-8").read()

block_u = '''
# ============================================================ БЛОК U: доводка R3

def block_u():
    print("\\n=== Блок U: ppm-развёртка Live, провалы, кеш, XRUN ===")
    fs = 48000

    # U1: Live при рассогласовании тактов ±5/±10/±21/±50 ppm — чистый синус
    # не бракуется из-за одного лишь рассогласования, ΔG ≈ 0
    rows = []
    all_ok = True
    for f0 in (997.0, 1000.0, 10000.0):
        for ppm in (0, 5, 10, 21, 50, -5, -10, -21, -50):
            f_real = f0 * (1.0 + ppm * 1e-6)
            n = int(0.6 * fs)
            t = np.arange(n) / fs
            sig = amp_of(-20.0) * np.sin(2 * math.pi * f_real * t)
            rng = np.random.default_rng(int(abs(ppm)) + 60)
            nz = band_limited_noise(n, fs, "white", -110.0, rng)
            ring = np.column_stack([sig + nz, sig + nz])
            tr = gainmatch.GainTracker()
            st = tr.update(ring, fs, f0, -20.0, 0, None, now=0.0)
            derr = abs(st["delta"]) if st["valid"] else None
            rows.append((f0, ppm, st["valid"], derr))
            if ppm != 0 and (not st["valid"]):
                all_ok = False
            if st["valid"] and ppm != 0 and derr > 0.02:
                all_ok = False
            print(f"      {f0:6.0f} Гц, {ppm:+3d} ppm: valid={st['valid']}, "
                  f"ΔG={'—' if derr is None else '%.4f' % derr}, "
                  f"C/R={['%.1f' % v for v in st.get('carrier_residual_db', [0])] if st['valid'] else '—'}, "
                  f"msg={st['msg'][:40] if not st['valid'] else ''}")
    check("U1", "Live при рассогласовании ±5…±50 ppm (997/1к/10к): чистый синус "
                "не бракуется, ΔG < 0.02 дБ", "ok", all_ok,
          "частота трекается по медианному фазовому набегу — расстройка тактов "
          "не попадает в остаток (закрывает расчёт координатора: 21 ppm@10к "
          "давал бы C/R 14 дБ при фиксированной частоте)",
          "см. таблицу выше")

    # U2: провалы амплитуды 5–50 мс в кольце Live (отрицательные выбросы)
    fails_ok = True
    for dur in (0.005, 0.020, 0.050):
        n = int(0.6 * fs)
        t = np.arange(n) / fs
        sig = amp_of(-20.0) * np.sin(2 * math.pi * 1000.0 * t)
        i0 = int(0.25 * fs)
        sig[i0:i0 + int(dur * fs)] = 0.0            # провал (не всплеск!)
        rng = np.random.default_rng(77)
        nz = band_limited_noise(n, fs, "white", -110.0, rng)
        ring = np.column_stack([sig + nz, sig + nz])
        tr = gainmatch.GainTracker()
        st = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=0.0)
        zmax = max(s["z_burst"] for s in st.get("envelope", [{}])) if st.get("envelope") else 0
        if st["valid"]:
            fails_ok = False
        print(f"      провал {dur * 1000:.0f} мс: valid={st['valid']}, "
              f"msg={st['msg'][:44]}, z={zmax:.1f}")
    check("U2", "Live: кратковременные ПРОВАЛЫ амплитуды 5–50 мс бракуются "
                "(симметричный z огибающей)", "ok", fails_ok,
          "ожидание: invalid через unstable_carrier — z симметричен по знаку",
          "см. выше")

    # U3: XRUN-дельта Live (чистая функция)
    f_ok1, nx1, na1 = gainmatch.live_xrun_state(None, None, 10.0, 5, hold_s=1.0)
    f_ok2, nx2, na2 = gainmatch.live_xrun_state(5, None, 11.0, 5, hold_s=1.0)
    f_ok3, nx3, na3 = gainmatch.live_xrun_state(5, 10.0, 11.2, 7, hold_s=1.0)
    f_ok4, nx4, na4 = gainmatch.live_xrun_state(7, 11.2, 13.0, 7, hold_s=1.0)
    f_ok5, _, _ = gainmatch.live_xrun_state(7, 11.2, 25.0, 7, hold_s=1.0)
    check("U3", "Live XRUN: приращение счётчика инвалидирует на 1 с; старое — не блокирует",
          "ok", (not f_ok1) and (not f_ok2) and f_ok3 and f_ok4 and (not f_ok5),
          "первое значение — базовая линия; без приращения — валидно; "
          "приращение → invalid на 1 с; спустя 1.5 с — снова валидно",
          f"база:{not f_ok1}, без изменений:{not f_ok2}, новое XRUN:{f_ok3}, "
          f"спустя 1.8 с:{f_ok4}, спустя 13.8 с:{not f_ok5}")

    # U4: кеш pinv — ограничен, hit/miss статистика, время hit/miss
    tr = gainmatch.GainTracker()
    times_hit, times_miss = [], []
    freqs_seq = [100.0 + i for i in range(30)]        # вращение частоты → miss
    for i, f_i in enumerate(freqs_seq):
        n = int(0.6 * fs)
        t = np.arange(n) / fs
        sig = amp_of(-20.0) * np.sin(2 * math.pi * f_i * t)
        ring_i = np.column_stack([sig, sig])
        tr.update(ring_i, fs, f_i, -20.0, 0, None, now=0.1 * i)
    # замер miss: новая частота
    t0 = time.perf_counter()
    f_new = 500.0
    n = int(0.6 * fs)
    t = np.arange(n) / fs
    sig = amp_of(-20.0) * np.sin(2 * math.pi * f_new * t)
    ring_n = np.column_stack([sig, sig])
    tr.update(ring_n, fs, f_new, -20.0, 0, None, now=99.0)
    t_miss = (time.perf_counter() - t0) * 1000
    # замер hit: повтор той же частоты
    t0 = time.perf_counter()
    tr.update(ring_n, fs, f_new, -20.0, 0, None, now=99.1)
    t_hit = (time.perf_counter() - t0) * 1000
    cache_len = len(gainmatch._PINV_CACHE)
    # U5: p50/p95/p99 при вращении частоты (частично miss/hit)
    p = []
    for i in range(40):
        f_i = 600.0 + (i % 7)
        t = np.arange(n) / fs
        sig = amp_of(-20.0) * np.sin(2 * math.pi * f_i * t)
        ring_i = np.column_stack([sig, sig])
        t0 = time.perf_counter()
        tr.update(ring_i, fs, f_i, -20.0, 0, None, now=200.0 + 0.1 * i)
        p.append((time.perf_counter() - t0) * 1000)
    p.sort()
    p50, p95, p99 = p[19], p[min(37, len(p) - 1)], p[-1]
    stats = dict(gainmatch._PINV_STATS)
    check("U4", "Кеш pinv: LRU ограничен 8 профилями (~34 МиБ max), hit/miss считаются",
          "ok", cache_len <= 8 and stats["hit"] > 0 and stats["miss"] > 0,
          "ожидание: len ≤ 8 при 30+ разных частотах; hit и miss растут",
          f"len={cache_len}; hit={stats['hit']}, miss={stats['miss']}")
    check("U5", "Замеры update(): miss ≤ 60 мс (единичный), p99 цикла ≤ 60 мс",
          "ok", t_miss <= 60.0 and p99 <= 60.0 and t_hit < 10.0,
          "ожидание: miss (построение базиса+pinv) — единичный джиттер кадра; "
          "hit — единицы мс; p99 цикла вращения ≤ 60 мс",
          f"miss={t_miss:.1f} мс, hit={t_hit:.1f} мс, "
          f"p50={p50:.1f}, p95={p95:.1f}, p99={p99:.1f} мс")


'''

anchor = "# ============================================================ main"
assert anchor in src
src = src.replace(anchor, block_u + "\n" + anchor)
src = src.replace(
    """    block_t()
    n_all = len(RESULTS)""",
    """    block_t()
    block_u()
    n_all = len(RESULTS)""")
io.open(P, "w", encoding="utf-8").write(src)
print("block U added")
