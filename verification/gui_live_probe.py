#!/usr/bin/env python3
"""Контрольная приёмка fix3, пункты 3-6: настоящий GTK4 на реальном рабочем столе.

Запускает ПОЛНОЦЕННОЕ приложение (тот же ui.main(), реальные E-MU 0404 USB и
Wayland) с измерительными зондами БЕЗ изменений продуктового кода:
  * обёртки таймеров страниц (LivePage.tick 75 мс, SpectrumPage.tick,
    PeakHoldPage.tick) и _apply — длительности колбэков;
  * GdkFrameClock главного окна — частота кадров UI и худшие провалы
    (отзывчивость интерфейса, а не только сумма CPU);
  * фоновый сэмплер /proc — CPU по потокам процесса (GUI/cap/play/monitor)
    и дочерним процессам (arecord/aplay/analysis-worker) по состояниям;
  * XRUN-счётчик и события бэкенда на границах состояний.

Состояния (по заданию координатора):
  S1 приложение открыто, аудио выключено
  S2 аудиопоток работает, генератор выключен (страница Gain Match)
  S3 генератор включён, открыта Gain Match
  S4 генератор включён, открыт Спектр
  S5 генератор включён, открыт Peak Hold
  S6 страница THD (спектр и peak hold СКРЫТЫ — проверка, что расчёт остановился)
  S7 как S3/S4, но 96 кГц

Окно открывается на рабочем столе и закрывается само по завершении.
"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("CLS", "")

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from valvebench import ui  # noqa: E402

EMU_USBID = "041e:3f04"   # E-MU 0404 USB; fallback — любая duplex-карта
PHASE_S = {"S1": 8, "S2": 10, "S3": 15, "S4": 15, "S5": 15, "S6": 10,
           "S7_gm": 12, "S7_spec": 12}
_scale = float(os.environ.get("VB_PROBE_SCALE", "1"))
if _scale != 1.0:
    PHASE_S = {k: max(3, int(v * _scale)) for k, v in PHASE_S.items()}

win = None
app = None
buckets = {}          # имя колбэка → [мс]
phase_snapshots = {}  # фаза → (cpu_snapshot, wall, xruns, tick_числа)
phase_order = []
current_phase = [None]
frame_deltas = []
timeout_jitter = []
frame_cb_id = [None]
RESULTS = {"phases": {}, "meta": {}}


# ---- зонд 1: обёртки колбэков ----------------------------------------------
def wrap_callbacks():
    def wrap(inst, name):
        orig = getattr(inst, name)
        b = buckets.setdefault(f"{type(inst).__name__}.{name}", [])
        def w(*a, **k):
            t0 = time.perf_counter()
            try:
                return orig(*a, **k)
            finally:
                b.append((time.perf_counter() - t0) * 1000.0)
        setattr(inst, name, w)
    for inst, meth in ((win.live, "tick"), (win.live, "apply"),
                       (win.live, "_update_delta"),
                       (win.spectrum, "tick"), (win.spectrum, "_apply"),
                       (win.peakhold, "tick"), (win.peakhold, "_apply")):
        wrap(inst, meth)


# ---- зонд 2: кадровая частота UI -------------------------------------------
def install_frame_probe():
    # ВНИМАНИЕ: add_tick_callback заставляет кадровый цикл работать на
    # максимальной частоте непрерывно — это замер ХУДШЕГО случая (принудительные
    # 60 fps), а не естественная перерисовка по damage. По умолчанию выключен;
    # включается VB_PROBE_FRAMES=1 для стресс-оценки.
    if os.environ.get("VB_PROBE_FRAMES", "0") != "1":
        return
    def tick_cb(_w, frame_clock):
        frame_deltas.append(frame_clock.get_frame_time() / 1000.0)  # мкс → мс
        return True
    frame_cb_id[0] = win.add_tick_callback(tick_cb)


def install_timeout_probe():
    state = {"last": None, "n": 0}
    def cb():
        t = time.monotonic() * 1000.0
        if state["last"] is not None:
            timeout_jitter.append(t - state["last"])
        state["last"] = t
        state["n"] += 1
        if state["n"] % 2000 == 0:
            print(f"  [probe] timeout-сэмплов: {state['n']}", flush=True)
        if len(frame_deltas) > 400000:
            del frame_deltas[:200000]
        return True
    GLib.timeout_add(17, cb)


# ---- зонд 2b: стеки главного потока (что именно ест GTK main) ---------------
stack_samples = []

def install_stack_sampler():
    import collections
    def sampler():
        me = threading.main_thread().ident
        while True:
            time.sleep(1.0)
            try:
                fr = sys._current_frames().get(me)
            except Exception:
                continue
            if fr is None:
                continue
            chain = []
            f = fr
            while f is not None and len(chain) < 12:
                code = f.f_code
                chain.append(f"{code.co_filename.split('/')[-1]}:{code.co_name}")
                f = f.f_back
            stack_samples.append((current_phase[0],
                                  " <- ".join(reversed(chain))))
    t = threading.Thread(target=sampler, daemon=True)
    t.start()


def stack_summary():
    import collections
    # пофазно: агрегируем по нижним 3 кадрам (самое глубокое в продукте)
    per = collections.defaultdict(collections.Counter)
    for phase, s in stack_samples:
        parts = s.split(" <- ")
        per[phase][" <- ".join(parts[-3:])] += 1
    return per


def busy_share(phase_counters):
    busy = sum(n for chain, n in phase_counters.items()
               if "Gio.py:run <- gui_live_probe" not in chain
               and not chain.startswith("gui_live_probe.py:<module> <- gui_live_probe.py:main <- Gio.py:run"))
    total = sum(phase_counters.values())
    return busy, total


# ---- зонд 3: CPU по потокам/процессам ---------------------------------------
PID = os.getpid()
CLK = os.sysconf("SC_CLK_TCK")


def _ticks(path):
    try:
        f = open(path).read()
        fl = f[f.rindex(")") + 2:].split()
        return (int(fl[11]) + int(fl[12])) / CLK
    except (OSError, ValueError, IndexError):
        return 0.0


def cpu_snapshot():
    threads = {}
    for tid in os.listdir(f"/proc/{PID}/task"):
        try:
            comm = open(f"/proc/{PID}/task/{tid}/comm").read().strip()
        except OSError:
            comm = "?"
        threads[str(tid)] = (_ticks(f"/proc/{PID}/task/{tid}/stat"), comm)
    children = {}
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
            children[name] = children.get(name, 0.0) + _ticks(f"/proc/{d}/stat")
    return {"threads": threads, "children": children,
            "t": time.monotonic(),
            "n_threads": len(threads)}


def xrun_state():
    try:
        st = win.be.live_status() if win.be else {}
    except Exception:
        st = {}
    ev = 0
    try:
        ev = len(win.be.live_events()) if win.be else 0
    except Exception:
        pass
    return {"xruns": st.get("xruns"), "events": ev,
            "capture_alive": st.get("capture_alive"),
            "playback_alive": st.get("playback_alive")}


def phase_begin(name):
    current_phase[0] = name
    phase_order.append(name)
    phase_snapshots[name] = [cpu_snapshot(), xrun_state(), time.monotonic()]


def phase_end(name):
    snap0, xrun0, _t0 = phase_snapshots[name]
    snap1 = cpu_snapshot()
    xrun1 = xrun_state()
    dur = snap1["t"] - snap0["t"]
    threads, children = {}, {}
    for tid, (cpu, comm) in snap1["threads"].items():
        d = (cpu - snap0["threads"].get(tid, (0.0, ""))[0]) / dur * 100
        if d >= 0.05:
            if "_cap" in comm:
                key = "cap_loop"
            elif "_play" in comm:
                key = "play_loop"
            elif "_moni" in comm:
                key = "monitor_loop"
            elif tid == str(PID):
                key = "GTK main"
            else:
                key = f"прочий ({comm[:12]})"
            threads[key] = round(threads.get(key, 0.0) + d, 1)
    for name_c, cpu in snap1["children"].items():
        d = (cpu - snap0["children"].get(name_c, 0.0)) / dur * 100
        if d >= 0.05:
            children[name_c] = round(d, 1)
    RESULTS["phases"][name] = {
        "dur_s": round(dur, 1),
        "threads_pct": threads,
        "children_pct": children,
        "proc_total_pct": round(sum(threads.values()), 1),
        "child_total_pct": round(sum(children.values()), 1),
        "xruns": (xrun0["xruns"], xrun1["xruns"]),
        "events": (xrun0["events"], xrun1["events"]),
        "n_threads": (snap0["n_threads"], snap1["n_threads"]),
    }
    print(f"  {name}: {dur:.0f} с, процесс {RESULTS['phases'][name]['proc_total_pct']}% "
          f"+ дети {RESULTS['phases'][name]['child_total_pct']}% "
          f"xruns {xrun0['xruns']}→{xrun1['xruns']}", flush=True)


def pct(vals, q):
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * q))] * 1.0


# ---- сценарий ---------------------------------------------------------------
def _identity_of(hw):
    try:
        from valvebench.backend import stable_device_identity
        return stable_device_identity(hw)
    except Exception:
        return ""


def select_emu():
    """Строго E-MU по стабильной USB-идентификации (номер карты волатилен).

    Без E-MU — USB duplex-карта как fallback (у встроенных PCH/HDMI нет
    loopback: захват тишины обесценивает замер); прогон помечается.
    """
    for i, (hw, label) in enumerate(win.cards):
        if EMU_USBID in _identity_of(hw):
            win.dev.set_selected(i)
            print(f"устройство (E-MU): {hw} ({label})", flush=True)
            return True
    for i, (hw, label) in enumerate(win.cards):
        if "usb:" in _identity_of(hw):
            win.dev.set_selected(i)
            RESULTS["meta"]["emu_fallback"] = f"{hw} ({label})"
            print(f"УСТРОЙСТВО (E-MU НЕ НАЙДЕНА, fallback): {hw} ({label})",
                  flush=True)
            return True
    return False


def set_rate(fs):
    if fs in win.rate_values:
        win.rate.set_selected(win.rate_values.index(fs))


def at(sec, fn):
    GLib.timeout_add(int(sec * 1000), fn)


def _sched(dt_s, fn):
    """Одноразовый таймер GLib: выполнить fn через dt_s секунд."""
    def run():
        try:
            fn()
        finally:
            return False
    GLib.timeout_add(int(dt_s * 1000), run)


def start_drive():
    t = [0.0]
    def step(dt, fn):
        t[0] += dt
        _sched(t[0], fn)
    # S1: приложение открыто, аудио выключено
    step(0.5, lambda: (phase_begin("S1"), print("S1: приложение открыто, аудио выкл", flush=True)))
    step(PHASE_S["S1"], lambda: (phase_end("S1"), select_emu()))
    # S2: поток включён, генератор выключен
    step(1.0, lambda: (phase_begin("S2"), win.live_btn.set_active(True),
                       print("S2: поток открыт, генератор выкл", flush=True)))
    step(2.0, lambda: print(f"  live_running={win.be.live_running if win.be else None}",
                            flush=True))
    step(PHASE_S["S2"] - 2.0, lambda: (phase_end("S2"), win.generator_btn.set_active(True)))
    # S3: генератор включён, Gain Match
    step(2.0, lambda: (phase_begin("S3"),
                       print("S3: генератор + Gain Match", flush=True),
                       RESULTS["meta"].__setitem__("generator_settings",
                                                   str(win.live.last_generator_settings))))
    step(PHASE_S["S3"], lambda: (phase_end("S3"), win.stack.set_visible_child(win.page_map["spectrum"])))
    # S4: спектр
    step(2.0, lambda: (phase_begin("S4"), print("S4: генератор + Спектр", flush=True)))
    step(PHASE_S["S4"], lambda: (phase_end("S4"), win.stack.set_visible_child(win.page_map["peakhold"])))
    # S5: peak hold
    step(2.0, lambda: (phase_begin("S5"), print("S5: генератор + Peak Hold", flush=True)))
    step(PHASE_S["S5"], lambda: (phase_end("S5"), win.stack.set_visible_child(win.page_map["thd"])))
    # S6: скрытые spectrum/peakhold — расчёт должен остановиться
    step(2.0, lambda: (phase_begin("S6"), print("S6: страница THD, спектр/peakhold скрыты", flush=True)))
    step(PHASE_S["S6"], lambda: (phase_end("S6"), set_rate(96000)))
    # S7: 96 кГц (rate change перезапускает rig: live выключится сам)
    step(2.5, lambda: (print("S7: 96 кГц", flush=True), win.live_btn.set_active(True)))
    step(1.5, lambda: (phase_begin("S7_gm"), win.generator_btn.set_active(True)))
    step(2.0, lambda: print(f"  96k live={win.be.live_running if win.be else None}, "
                            f"fs={win.rig_fs()}", flush=True))
    step(PHASE_S["S7_gm"], lambda: (phase_end("S7_gm"),
                                    win.stack.set_visible_child(win.page_map["spectrum"])))
    step(2.0, lambda: phase_begin("S7_spec"))
    step(PHASE_S["S7_spec"], lambda: (phase_end("S7_spec"), finish()))


def finish():
    # сводка по колбэкам и кадрам (по фазам не делим — даём общую картину + в фазах CPU)
    cb_summary = {}
    for name, arr in buckets.items():
        if arr:
            cb_summary[name] = {"n": len(arr),
                                "p50_ms": round(pct(arr, 0.5), 2),
                                "p95_ms": round(pct(arr, 0.95), 2),
                                "max_ms": round(max(arr), 2)}
    fd = [b - a for a, b in zip(frame_deltas, frame_deltas[1:])
          if 0 < b - a < 1000] if frame_deltas else []
    tj = [x for x in timeout_jitter if 0 < x < 1000]
    RESULTS["callbacks"] = cb_summary
    per = stack_summary()
    RESULTS["main_thread_stacks"] = {p: dict(c) for p, c in per.items()}
    print("\n=== ГЛАВНЫЙ ПОТОК по фазам: занятость (небездействие) + топ ===")
    for p, c in per.items():
        busy, total = busy_share(c)
        tops = "; ".join(f"{chain.split(' <- ')[-1]}:{n}" for chain, n in c.most_common(3))
        print(f"  {p}: busy {busy}/{total} сэмплов | {tops}")
    RESULTS["ui"] = {
        "frames_n": len(fd),
        "frame_interval_p50_ms": round(pct(fd, 0.5), 2) if fd else None,
        "frame_interval_p95_ms": round(pct(fd, 0.95), 2) if fd else None,
        "frame_gap_max_ms": round(max(fd), 2) if fd else None,
        "frames_gap_gt100ms": sum(1 for x in fd if x > 100),
        "timeout16_p50_ms": round(pct(tj, 0.5), 2) if tj else None,
        "timeout16_p95_ms": round(pct(tj, 0.95), 2) if tj else None,
        "timeout16_max_ms": round(max(tj), 2) if tj else None,
    }
    try:
        if win.be:
            RESULTS["meta"]["final_live_status"] = {k: str(v) for k, v in win.be.live_status().items()}
    except Exception as exc:
        RESULTS["meta"]["final_live_status"] = f"err {exc}"
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui-live-probe.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(RESULTS, fh, ensure_ascii=False, indent=1)
    print("JSON:", out, flush=True)
    print("\n=== ОТЗЫВЧИВОСТЬ UI ===")
    for k, v in RESULTS["ui"].items():
        print(f"  {k}: {v}")
    print("\n=== КОЛБЭКИ ===")
    for k, v in cb_summary.items():
        print(f"  {k}: n={v['n']} p50={v['p50_ms']} p95={v['p95_ms']} max={v['max_ms']} мс")
    GLib.timeout_add(500, app.quit)
    return False


def wait_window():
    """App теперь сам держит единственное окно (fix4): ждём его, не создаём второе.

    Прежняя проба подключала свой activate-хендлер рядом с лямбдой App — оба
    создавали окно, и на столе пользователя появлялись два окна за один запуск.
    """
    global win
    if app._window is None:
        return True  # окно ещё не создано — ждём дальше
    win = app._window
    wrap_callbacks()
    install_frame_probe()
    install_timeout_probe()
    install_stack_sampler()
    RESULTS["meta"]["display"] = {k: os.environ.get(k) for k in
                                  ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE")}
    RESULTS["meta"]["cards"] = [hw for hw, _ in win.cards]
    if not select_emu():
        print("FAIL: E-MU (hw:2,*) не найдена в списке устройств", flush=True)
        RESULTS["meta"]["error"] = "нет E-MU"
        app.quit()
        return False
    def _begin():
        start_drive()
        return False
    GLib.timeout_add(2500, _begin)
    return False


def main():
    global app
    Adw.init()
    app = ui.App()
    GLib.timeout_add(200, wait_window)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
