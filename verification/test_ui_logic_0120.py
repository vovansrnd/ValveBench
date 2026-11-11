"""Логика страниц 0.1.20 без GTK: заглушка gi, страницы создаются в обход __init__.

Проверяется то, что можно проверить без дисплея: отбраковка кадров, порядок/видимость
каналов, усреднение, панель анализа и маркеры, ΔG-блокировка, полоса уровней и плашка
состояния. Вёрстку и сигналы GTK это НЕ проверяет."""
import sys, os, math, time, types
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import stubgi; stubgi.install()
from valvebench import ui, dsp, analysis, livemetrics as lm

OK = 0
def check(name, cond, info=""):
    global OK
    print(("PASS  " if cond else "FAIL  ") + name + (f": {info}" if info else ""))
    if cond: OK += 1
    else: raise SystemExit(1)

class Sel:
    def __init__(s, v): s.v = v
    def get_selected(s): return s.v
class Chk:
    def __init__(s, v): s.v = v
    def get_active(s): return s.v
class Lab:
    def __init__(s): s.t = ""; s.cls = set()
    def set_text(s, t): s.t = t
    def add_css_class(s, c): s.cls.add(c)
    def remove_css_class(s, c): s.cls.discard(c)
class FPlot:
    xlog = True; ylim = (-200.0, 0.0)
    def __init__(s): s.traces = None; s.overlay = None; s.markers = None; s.view = None
    def set_view(s, xlim=None, ylim=None, **k):
        s.view = (xlim, ylim)
        if ylim: s.ylim = ylim
    def set_traces(s, t): s.traces = t
    def set_overlay(s, f): s.overlay = f
    def set_markers(s, m): s.markers = m
    def set_visible(s, v): pass
class FBe:
    live_running = True
    def __init__(s): s.events = []
    def live_events(s, since=0): return [e for e in s.events if e["suspect"][1] >= since]
    def live_status(s): return {"xruns": len(s.events), "capture_xruns": 0, "playback_xruns": 0,
                               "capture_alive": True, "playback_alive": True}
class FWin:
    def __init__(s):
        s.be = FBe(); s.notes = []
        s.analysis = types.SimpleNamespace(mode="process") if False else None
        import types as _t
        s.analysis = _t.SimpleNamespace(mode="process")
        s.live = _t.SimpleNamespace(last_generator_settings=(1000.0, -6.0, 0))
    def rig_fs(s): return 192000
    def post_health(s, key, level, text, ttl=6.0): s.notes.append((key, level, text))
    def get_noise_floor_profile(s, fs): return None

def mk_page(front=0, vis=(True, True), show_bad=False, mode=1):
    p = ui.SpectrumPage.__new__(ui.SpectrumPage)
    p.win = FWin(); p.fft = Sel(5); p.mode = Sel(mode); p.floor = Sel(3); p.top = Sel(0); p.band = Sel(2)
    p.avg_mode = Sel(1); p.chk_l = Chk(vis[0]); p.chk_r = Chk(vis[1]); p.front = Sel(front)
    p.chk_diff = Chk(True); p.chk_card = Chk(True); p.chk_bad = Chk(show_bad)
    p.banner = Lab(); p._banner_class = None; p.state = Lab()
    p.plot = FPlot(); p.diff_plot = FPlot()
    p.avg_line_power = None; p.avg_bin_power = None; p.rejected = 0; p._frames_ok = 0
    return p

fs, n = 192000, 262144
rng = np.random.default_rng(21)
t = np.arange(n) / fs
def make(h2l=-86, h2r=-71, drop=False):
    def ch(h2):
        x = 0.3*np.sin(2*np.pi*1000*t) + 0.3*10**(h2/20)*np.sin(2*np.pi*2000*t+1)
        return x + 1e-6*rng.standard_normal(n)
    X = np.column_stack([ch(h2l), ch(h2r)])
    if drop: X[130000:130000+16500, 0] = 0
    return X
def job(X): return analysis.spectrum_job(X, fs, n, 1000.0, (True, True))
meta = {"end": 10 * n, "n": n, "fs": fs, "f_hint": 1000.0}

# 1. чистый кадр
p = mk_page()
res = job(make())
p._apply(res, meta)
check("clean frame is drawn: two traces, R in front", [tr["label"] for tr in p.plot.traces] == ["L", "R"] and p.rejected == 0)
check("PSD mode uses power-mean decimation", all(tr["decim"] == "mean" for tr in p.plot.traces))
check("THD card and harmonic markers are produced", p.plot.overlay is not None and len(p.plot.markers) >= 2,
      f"{len(p.plot.markers)} markers")
check("L-R strip shows the 2nd harmonic dip (R has +15 dB H2)",
      float(np.min(p.diff_plot.traces[0]["y"][(p.diff_plot.traces[0]["x"] > 1800) & (p.diff_plot.traces[0]["x"] < 2300)])) < -10)
rows = ui.format_analysis_rows(*res["analysis"])
h2 = [r for r in rows if r["label"].startswith("H2")][0]["cells"]
check("per-channel H2 readings differ as built (-86 vs -71 dBc)", h2[0].startswith("-86") and h2[1].startswith("-71"), str(h2))

# 2. порядок и видимость
p = mk_page(front=1); p._apply(res, meta)
check("front=L puts L last (drawn on top)", [tr["label"] for tr in p.plot.traces] == ["R", "L"])
p = mk_page(vis=(False, True)); p._apply(res, meta)
check("L hidden -> only R drawn", [tr["label"] for tr in p.plot.traces] == ["R"])

# 3. кадр с провалом несущей: отбракован, усреднение цело
p = mk_page()
p._apply(job(make()), meta)
before = p.avg_bin_power.copy(); traces_before = p.plot.traces
bad = job(make(drop=True))
p._apply(bad, meta)
check("frame with carrier dropout is rejected", p.rejected == 1 and p.plot.traces is traces_before)
check("rejected frame did not touch the averages", np.array_equal(before, p.avg_bin_power))
check("health note posted as 'bad'", p.win.notes and p.win.notes[-1][1] == "bad" and "провал" in p.win.notes[-1][2], p.win.notes[-1][2])
check("banner shows the event", "error" in p.banner.cls and "провал несущей" in p.banner.t, p.banner.t)

# 4. включён показ кадров с событиями: красные, не усредняются
p = mk_page(show_bad=True); p._apply(job(make()), meta); before = p.avg_bin_power.copy()
p._apply(bad, meta)
check("show-bad: drawn in red, not averaged", all(tr["color"] == (0.90, 0.25, 0.25) for tr in p.plot.traces)
      and np.array_equal(before, p.avg_bin_power))

# 5. XRUN backend-события в окне
p = mk_page(); p.win.be.events.append({"kind": "xrun_capture", "frame": 10*n-1000, "t": time.monotonic(),
    "suspect": (10*n - 20000, 10*n + 3000), "fatal": True, "dur_ms": 84.6})
p._apply(job(make()), meta)
check("backend XRUN inside the window rejects a data-clean frame", p.rejected == 1 and "XRUN АЦП" in p.banner.t, p.banner.t)
p = mk_page(); p.win.be.events.append({"kind": "xrun_capture", "frame": 3*n, "t": time.monotonic(),
    "suspect": (3*n - 20000, 3*n + 3000), "fatal": True})
p._apply(job(make()), meta)
check("old XRUN outside the window does not reject", p.rejected == 0)

# 6. смена размера FFT между съёмом и показом — кадр игнорируется
p = mk_page(); p.fft = Sel(4); p._apply(res, meta)
check("stale-size result is dropped", p.plot.traces is None)

# 7. полоса уровней/плашка: чистые функции
level, text = lm.health_summary(None, {"xruns": 0}, [], 100.0)
check("health ok when clean", level == "ok")
ev = [{"kind": "xrun_capture", "t": 98.0, "fatal": True, "dur_ms": 84.6, "suspect": (0, 1)}]
level, text = lm.health_summary(None, {"xruns": 1}, ev, 100.0)
check("health bad with recent XRUN", level == "bad" and "85" in text and "2 с" in text, text)
level, text = lm.health_summary("нет свежих отсчётов", {}, [], 100.0)
check("health bad on transport problem", level == "bad")
check("merge_health prefers the more severe", lm.merge_health(("ok", "a"), ("bad", "b"))[0] == "bad")

print(f"\n{OK} проверок пройдено")

# ======================================================== LivePage: ΔG и полоса уровней
from valvebench import calib, gainmatch
SIM = [0.0]      # модельные часы: время идёт вместе с позицией захвата, а не по стене
ui.GLib = types.SimpleNamespace(get_monotonic_time=lambda: int(SIM[0] * 1e6))
def advance(lp, be, ticks=1):
    for _ in range(ticks):
        be.total += int(0.075 * be.fs); SIM[0] = be.total / be.fs; lp.tick()

class FMeters:
    def __init__(s): s.rms = [None, None]; s.hold = [-120.0, -120.0]; s.clipped = [False, False]
    stale = None; delta = (None, None)
    def set_reading(s, c, r, p, clip=False):
        s.stale = False; s.rms[c] = r; s.hold[c] = max(s.hold[c], p); s.clipped[c] = s.clipped[c] or clip
    def set_stale(s, text=""): s.stale = text; s.delta = (None, "none")
    def set_delta(s, v, kind): s.delta = (v, kind)
    def reset_holds(s): pass
class FStrip:
    def __init__(s): s.meters = FMeters(); s.health = None
    def set_health(s, level, text): s.health = (level, text)

class RingBe(FBe):
    """Backend с кольцом: захват тона 1 кГц -6 dBFS в оба канала, позиция растёт."""
    def __init__(s, fs=192000):
        super().__init__(); s.fs = fs; s.total = 10 * fs; s.drop = None; s.stale = False
    def live_recent(s, sec): return s.live_recent_stamped(sec)[0]
    def live_recent_stamped(s, sec):
        k = int(sec * s.fs); tt = (np.arange(k) + s.total - k) / s.fs
        x = 0.5 * np.sin(2 * np.pi * 1000 * tt) + 1e-7 * np.random.default_rng(5).standard_normal(k)
        X = np.column_stack([x, x * 10 ** (-0.014 / 20)])      # R на 0.014 дБ тише
        return X, s.total
    def live_status(s):
        d = super().live_status(); d.update(capture_frames=s.total, playback_frames=s.total,
            capture_stale=s.stale, playback_stale=False); return d

def mk_live(be):
    lp = ui.LivePage.__new__(ui.LivePage)
    lp.win = FWin(); lp.win.be = be; lp.win.strip = FStrip(); lp.win.cal = calib.Calibration()
    lp.win.toasts_ = []; lp.win.toast = lambda m: lp.win.toasts_.append(m)
    lp.win.refresh_health = lambda status: None
    lp.win.generator_btn = Chk(True); lp.win.live_btn = Chk(True)
    lp.tracker = gainmatch.GainTracker()
    lp.delta_view = types.SimpleNamespace(state=None, set_state=lambda st, tol: setattr(lp.delta_view, "state", st))
    lp.tol = types.SimpleNamespace(get_value=lambda: 0.2)
    lp.lv = types.SimpleNamespace(get_value=lambda: -6.0); lp.route = Sel(0)
    lp.last_generator_settings = (1000.0, -6.0, 0)
    lp._last_xruns = None; lp._last_xrun_at = None; lp._fresh_after_event_frames = None
    lp._delta_error_shown = False; lp._last_delta_state = None; lp._delta_blocked = True
    for nm in ("read_l", "read_r", "detail_l", "detail_r", "xrun_label", "clip_label", "status_label", "clock_label"):
        setattr(lp, nm, Lab())
    lp.timer = 1
    return lp

be = RingBe(); lp = mk_live(be)
# прогрев фильтра ΔG несколькими тиками
advance(lp, be, 25)
st = lp._last_delta_state
check("clean stream: ΔG validated and sent to the strip", st and st.get("valid") and lp.win.strip.meters.delta[1] == "validated",
      f"{lp.win.strip.meters.delta}")
check("strip delta matches the built-in 0.014 dB", abs(lp.win.strip.meters.delta[0] - 0.014) < 0.01, f"{lp.win.strip.meters.delta[0]:+.4f}")
check("RMS numbers reach the strip", lp.win.strip.meters.rms[0] is not None and abs(lp.win.strip.meters.rms[0] - (-6.02)) < 0.05,
      f"{lp.win.strip.meters.rms}")
check("no hidden exception in _update_delta", not lp.win.toasts_, str(lp.win.toasts_))
# XRUN-событие в окне ΔG 0.6 с
be.events.append({"kind": "xrun_capture", "frame": be.total - 1000, "t": time.monotonic(), "fatal": True, "dur_ms": 84.6,
                  "suspect": (be.total - 24000, be.total + 4000)})
advance(lp, be)
check("XRUN in the 0.6 s window blocks ΔG and clears the strip delta",
      lp._delta_blocked and lp.win.strip.meters.delta == (None, "none") and "Событие потока" in (lp.delta_view.state or {}).get("msg", ""),
      (lp.delta_view.state or {}).get("msg", ""))
# DAC underrun: дефект дойдёт до захвата позже — окно блокируется заранее
lp2 = mk_live(RingBe()); be2 = lp2.win.be
advance(lp2, be2, 25)
be2.events.append({"kind": "xrun_playback", "frame": be2.total, "t": time.monotonic(), "fatal": True,
                   "suspect": (be2.total - 19200, be2.total + int(1.2 * be2.fs))})
blocked_ticks = 0
for _ in range(60):                       # 4.5 с модельного времени
    advance(lp2, be2); blocked_ticks += lp2._delta_blocked
check("DAC underrun blocks ΔG until the suspect interval has left the 0.6 s window",
      14 <= blocked_ticks <= 40, f"blocked {blocked_ticks} of 60 ticks (~{blocked_ticks*0.075:.1f} s)")
check("ΔG recovers afterwards", not lp2._delta_blocked and lp2.win.strip.meters.delta[1] == "validated")
# нет свежих данных
lp3 = mk_live(RingBe()); lp3.win.be.stale = True
lp3.tick()
check("stale capture: strip shows stale, no delta", lp3.win.strip.meters.stale and lp3.win.strip.meters.delta == (None, "none"))
# генератор выключен: старое «проверенное» значение не остаётся
lp.win.generator_btn = Chk(False); lp.last_generator_settings = None
lp.tick()
check("generator off: no validated delta lingers", lp.win.strip.meters.delta[1] in ("none",), str(lp.win.strip.meters.delta))
print(f"\n{OK} проверок пройдено")
