#!/usr/bin/env python3
"""CPU-профиль ПОСЛЕ исправлений: состояния A-E по ТЗ координатора, на реальной карте.

Отличия от cpu_profile_0120.py (профиль «до»):
  * настоящий AlsaCli (3 рабочих потока в процессе GUI: cap_loop/play_loop/
    monitor_loop + дочерние arecord/aplay) вместо файловой имитации;
  * CPU разбит по потокам процесса и по дочерним процессам (/proc/*/task);
  * состояние D — настоящий spectrum_job через AnalysisWorker (дочерний
    процесс), как в ui.py, а не несуществующий dsp.spectrum;
  * состояние E — то же + rta_job Peak Hold (общий AnalysisWorker), как в ui.py.

Запуск:  OPENBLAS_NUM_THREADS=4 python3 cpu_profile_after.py before   # «до»
         python3 cpu_profile_after.py after                           # «после»
"""
import json
import math
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LABEL = sys.argv[1] if len(sys.argv) > 1 else "after"
if LABEL == "before":
    # Профиль «до»: многопоточный BLAS как был по умолчанию (4 ядра).
    # setdefault в __init__ уважит уже выставленное значение.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
    os.environ.setdefault("OMP_NUM_THREADS", "4")

import numpy as np  # noqa: E402  (импорт после env — как в продукте)

from valvebench import gainmatch  # noqa: E402
from valvebench.analysis import AnalysisWorker, spectrum_job  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402
from valvebench import peakhold as ph  # noqa: E402

FS = 48000
LEVEL = -6.0
TICK_S = 0.075          # таймер LivePage (ui.py:855)
SPECTRUM_MS = 500       # REFRESH_MS[262144] (ui.py:987)
PEAK_MS = 250           # троттлинг PeakHoldPage (ui.py:1858)
MEASURE_S = 15
PID = os.getpid()
CLK = os.sysconf("SC_CLK_TCK")
RESULTS = {}


def ticks_stat(stat_path):
    try:
        f = open(stat_path).read()
        fields = f[f.rindex(")") + 2:].split()
        return (int(fields[11]) + int(fields[12])) / CLK  # utime+stime после " comm)"
    except (OSError, ValueError, IndexError):
        return 0.0


def thread_table():
    """CPU-секунды по потокам процесса: {tid: (cpu, comm)}."""
    out = {}
    base = f"/proc/{PID}/task"
    for tid in os.listdir(base):
        try:
            comm = open(f"{base}/{tid}/comm").read().strip()
        except OSError:
            comm = "?"
        out[tid] = (ticks_stat(f"{base}/{tid}/stat"), comm)
    return out


def child_table():
    """CPU-секунды дочерних процессов: {name: cpu}, name из cmdline."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == PID:
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        name = ("arecord" if "arecord" in cmd else
                "aplay" if "aplay" in cmd else
                "analysis-worker" if "multiprocessing" in cmd else None)
        if name:
            out[name] = out.get(name, 0.0) + ticks_stat(f"/proc/{d}/stat")
    return out


def snapshot():
    return {"threads": thread_table(), "children": child_table(),
            "t": time.monotonic()}


def live_thread_roles(threads_before):
    """tid → имя роли для потоков, появившихся после live_start."""
    new = sorted(set(thread_table()) - set(threads_before), key=int)
    names = ["cap_loop", "play_loop", "monitor_loop"]
    return {tid: names[i] if i < len(names) else f"поток-{i + 1}"
            for i, tid in enumerate(new)}


def delta(s0, s1, dur, live_roles=None):
    """(CPU% по потокам с именами ролей, CPU% по дочерним).

    live_roles — отображение tid → имя для потоков, созданных live_start
    (diff множеств tid до/после; TID в Linux монотонны, порядок совпадает
    с порядком старта cap/play/monitor)."""
    threads = {}
    roles = {str(PID): "GUI (main)"}
    if live_roles:
        roles.update(live_roles)
    for tid, (cpu, comm) in s1["threads"].items():
        d = (cpu - s0["threads"].get(tid, (0.0, ""))[0]) / dur * 100
        if d >= 0.05:
            name = roles.get(tid)
            if name is None:
                # comm вида "Thread-2 (_cap_loop)" называет целевую функцию
                if "_cap" in comm:
                    name = "cap_loop"
                elif "_play" in comm:
                    name = "play_loop"
                elif "_moni" in comm:
                    name = "monitor_loop"
                else:
                    name = f"служебный ({comm[:9]})"
            threads[name] = round(threads.get(name, 0.0) + d, 1)
    children = {}
    for name, cpu in s1["children"].items():
        d = (cpu - s0["children"].get(name, 0.0)) / dur * 100
        if d >= 0.05:
            children[name] = round(d, 1)
    return threads, children


def run_state(name, be, tracker, worker, gen_on, spectrum, peak,
              live_roles=None):
    """Один замер: MEASURE_S секунд с реальными колбэками UI-таймеров."""
    s0 = snapshot()
    ticks, tick_ms = 0, []
    sub_last = 0.0
    peak_last = 0.0
    t0 = time.monotonic()
    now = t0
    while time.monotonic() - t0 < MEASURE_S:
        tick_start = time.perf_counter()
        # --- колбэк LivePage (75 мс): update в потоке GUI
        if gen_on:
            x = be.live_recent(0.6)
            tracker.update(x, FS, 1000.0, LEVEL, 0, None, now=now)
            ticks += 1
            tick_ms.append((time.perf_counter() - tick_start) * 1000)
        # --- колбэк SpectrumPage (100 мс, сабмит не чаще 500 мс) — дочерний процесс
        if spectrum and worker is not None and not worker.busy() and \
                (now - sub_last) * 1000 >= SPECTRUM_MS:
            x = be.live_recent(262144 / FS + 0.02)
            if len(x) >= 262144:
                worker.submit(spectrum_job, x[-262144:], FS, 262144, 1000.0,
                              (True, True), mains=50.0,
                              tag={"kind": "spectrum"})
                sub_last = now
        # --- колбэк PeakHoldPage (100 мс, сабмит не чаще 250 мс) — дочерний процесс
        if peak and worker is not None and not worker.busy() and \
                (now - peak_last) * 1000 >= PEAK_MS:
            x = be.live_recent(65536 / FS + 0.02)
            if len(x) >= 65536:
                worker.submit(ph.rta_job, x[-65536:], FS, 65536,
                              tag={"kind": "peak"})
                peak_last = now
        worker.poll() if worker is not None else None
        now += TICK_S
        time.sleep(max(0.0, TICK_S - (time.perf_counter() - tick_start)))
    s1 = snapshot()
    dur = s1["t"] - s0["t"]
    threads, children = delta(s0, s1, dur, live_roles)
    total_proc = sum(threads.values())
    total_child = sum(children.values())
    tick_ms.sort()
    p50 = tick_ms[len(tick_ms) // 2] if tick_ms else 0
    p95 = tick_ms[int(len(tick_ms) * 0.95)] if tick_ms else 0
    mx = tick_ms[-1] if tick_ms else 0
    line = (f"  {name}: процесс {total_proc:.1f}% + дети {total_child:.1f}% = "
            f"{total_proc + total_child:.1f}%")
    if tick_ms:
        line += f"; колбэк update: p50={p50:.1f} p95={p95:.1f} max={mx:.1f} мс"
    print(line, flush=True)
    return {"proc_pct": round(total_proc, 1), "child_pct": round(total_child, 1),
            "threads": threads, "children": children,
            "tick_p50_ms": round(p50, 1), "tick_p95_ms": round(p95, 1),
            "tick_max_ms": round(mx, 1)}


def main():
    print(f"CPU-профиль [{LABEL}] — AlsaCli, состояния A-E по ТЗ; "
          f"ядер: {os.cpu_count()}; BLAS threads: "
          f"{os.environ.get('OPENBLAS_NUM_THREADS', '(по умолчанию OpenBLAS)')}")
    be = AlsaCli(hw=os.environ.get("VALVEBENCH_HW", "hw:2,0"))
    be.open(FS)
    tracker = gainmatch.GainTracker()
    worker = AnalysisWorker()
    worker.warm_up()
    time.sleep(1.5)

    print("Состояние A (всё выключено):")
    RESULTS["A"] = run_state("A", be, tracker, None, False, False, False)

    print("Состояние B (поток включён, генератор выключен):")
    tids_before = set(thread_table())
    be.live_start()
    be.live_set_loop(None)
    time.sleep(1.0)
    LIVE_ROLES = live_thread_roles(tids_before)
    RESULTS["B"] = run_state("B", be, tracker, None, False, False, False)

    tone = (10 ** (LEVEL / 20) * np.sin(
        2 * np.pi * 1000.0 * np.arange(int(0.25 * FS)) / FS))
    print("Состояние C (поток + генератор + Gain Match, тик 75 мс):")
    be.live_set_loop(np.column_stack([tone, tone]))
    time.sleep(1.0)
    RESULTS["C"] = run_state("C", be, tracker, None, True, False, False, LIVE_ROLES)

    print("Состояние D (то же + спектр 262144 через AnalysisWorker, 500 мс):")
    RESULTS["D"] = run_state("D", be, tracker, worker, True, True, False, LIVE_ROLES)

    print("Состояние E (то же + Peak Hold rta_job, 250 мс):")
    RESULTS["E"] = run_state("E", be, tracker, worker, True, True, True, LIVE_ROLES)

    be.live_stop()
    worker.close()

    print("\n=== СВОДКА (процесс + дочерние) ===")
    for s in ("A", "B", "C", "D", "E"):
        r = RESULTS[s]
        th = ", ".join(f"{k}={v}%" for k, v in r["threads"].items())
        ch = ", ".join(f"{k}={v}%" for k, v in r["children"].items())
        print(f"{s}: {r['proc_pct'] + r['child_pct']:.1f}%  | потоки: {th or '—'}"
              f"  | дети: {ch or '—'}")
    bc = RESULTS["C"]["proc_pct"] + RESULTS["C"]["child_pct"] - \
        RESULTS["B"]["proc_pct"] - RESULTS["B"]["child_pct"]
    print(f"\nПрирост генератора+GainMatch (B→C): {bc:+.1f}%")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"cpu-profile-{LABEL}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"label": LABEL, "states": RESULTS, "delta_b_c": round(bc, 1),
                   "fs": FS, "tick_s": TICK_S, "spectrum_ms": SPECTRUM_MS,
                   "peak_ms": PEAK_MS}, fh, ensure_ascii=False, indent=1)
    print(f"JSON: {out}")


if __name__ == "__main__":
    main()
