"""Превью раскладки 0.1.20 без GTK: заглушка gi + PIL-имитация cairo.

Запуск:  python3 verification/render_preview.py  → verification/preview-*.png
Проверяет геометрию (верх шкалы и числа видны, панель помещается), а не пиксели настоящего GTK."""
import sys, os, math, types
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)

class _Color: red, green, blue = 0.92, 0.92, 0.92
class _Stub:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: _Stub()
    def get_color(self): return _Color()
class _Meta(type):
    def __getattr__(cls, n): return 0
def _cls(name): return _Meta(name, (_Stub,), {})
Gtk = types.SimpleNamespace(**{n: _cls(n) for n in ("DrawingArea", "Box", "Label", "Button", "GestureClick",
    "EventControllerMotion", "Justification", "Orientation")})
gi = types.ModuleType("gi"); gi.require_version = lambda *a, **k: None
rep = types.ModuleType("gi.repository"); rep.Gtk = Gtk
sys.modules.update({"gi": gi, "gi.repository": rep})

from fakecairo import FakeContext
from valvebench import dsp, livemetrics as lm
from valvebench.plot import Plot
from valvebench import widgets as W

fs, n = 192000, 262144
rng = np.random.default_rng(11)
t = np.arange(n) / fs
def chan(h2, h3, noise_dbfs=-112):
    f = 1000.37; a = 0.3
    x = a*np.sin(2*np.pi*f*t) + a*10**(h2/20)*np.sin(2*np.pi*2*f*t+1) + a*10**(h3/20)*np.sin(2*np.pi*3*f*t+2)
    sig = dsp.dbfs_to_amp(noise_dbfs)/math.sqrt(2)*math.sqrt(0.5*fs/20000)
    noise = sig*rng.standard_normal(n)
    # шумовая полка Σ-Δ выше 40 кГц
    F = np.fft.rfft(noise); f_ = np.fft.rfftfreq(n, 1/fs); F *= 1 + 12*np.clip((f_-40000)/56000, 0, 1)**2
    return x + np.fft.irfft(F, n)
xl, xr = chan(-86, -92), chan(-71, -90)
blocks = [dsp.Block(x, fs) for x in (xl, xr)]
f = blocks[0].freq[1:]
psd = [10*np.log10(np.maximum(2*b.P/(fs/n), 1e-30))[1:] for b in blocks]
res = [lm.analyze_live_tone(x[-131072:], fs, 1000.0) for x in (xl, xr)]
rows = W.format_analysis_rows(res[0], res[1])

# ---- 1. Спектр: основной график + полоса L−R + панель анализа
def render_plot(Wd, Hd, draw_card=True):
    p = Plot(True, "Частота, Гц", "Плотность шума, dBFS/√Гц", right_y_axis=False)
    p.xlim = (10.0, fs/2); p.ylim = (-170.0, 0.0)
    p.traces = []
    p.set_traces([{"x": f, "y": psd[0], "label": "L", "color": "L", "width": 1.4, "decim": "mean", "alpha": 0.95},
                  {"x": f, "y": psd[1], "label": "R", "color": "R", "width": 1.4, "decim": "mean", "alpha": 0.8}])
    marks = []
    for r_, col in zip(res, (W.CH_COLORS[0], W.CH_COLORS[1])):
        for h in r_["harmonics"][:4]:
            if h["credible"]:
                k = h["k"] - 1
                marks.append({"x": h["f"], "y": float(psd[res.index(r_)][k-2:k+3].max()), "text": f"{h['h']}", "color": col})
    marks.append({"x": res[0]["f0"], "y": float(psd[0].max()), "text": "1"})
    p.set_markers(marks)
    p.set_overlay(lambda cr, l, tp, w, h, fg: W.draw_analysis_card(cr, l, tp, w, h, fg, rows, "BH · N=131 072 · f₀ по МНК", compact=(h < 330)) if draw_card else None)
    cr = FakeContext(Wd, Hd, scale=2); p._draw(None, cr, Wd, Hd); return cr

def render_strip(Hd):
    cr = FakeContext(184, Hd, scale=2)
    class C: red = green = blue = 0.92
    W.draw_meters(cr, 184, Hd, C(), [-5.24, -5.25], [-3.1, -3.4], [-2.2, -3.0], [False, False], False, "", 0.014, "validated", 90.0)
    return cr

# композиция: [график 1200x560][полоса 184x560]; снизу полоса L−R
Hs = 560
comp_w, comp_h = 1200 + 184 + 24, Hs + 150 + 12
from PIL import Image
canvas = Image.new("RGB", (comp_w*2, comp_h*2), (36, 36, 36))
main = render_plot(1200, Hs); canvas.paste(main.img.convert("RGB"), (0, 0))
# полоса L−R

def oct_smooth(f_, p_, per_oct=6):
    lf = np.log2(f_); b = np.floor((lf - lf[0]) * per_oct).astype(int)
    cnt = np.bincount(b).astype(float); s = np.bincount(b, weights=p_); fc = np.bincount(b, weights=f_)
    ok = cnt > 0
    return fc[ok]/cnt[ok], s[ok]/cnt[ok]
PL, PR = (blocks[0].P[1:], blocks[1].P[1:])
fc, a = oct_smooth(f, PL); _, b = oct_smooth(f, PR)
diff = 10*np.log10(a/b)
sp = Plot(True, "Частота, Гц", "L−R, дБ"); sp.xlim = (10.0, fs/2); sp.ylim = (-12.0, 12.0)
sp.set_traces([{"x": fc, "y": diff, "label": "L−R", "color": "ref", "width": 1.6}])
c2 = FakeContext(1200, 150, scale=2); sp._draw(None, c2, 1200, 150); canvas.paste(c2.img.convert("RGB"), (0, Hs*2 + 8))
strip = render_strip(Hs + 150 + 8); canvas.paste(strip.img.convert("RGB"), ((1200 + 24)*2, 0))
canvas.save(os.path.join(HERE, "preview-spectrum.png"))
# отдельно полоса уровней в трёх состояниях
imgs = []
for state in ("normal", "clip", "stale"):
    cr = FakeContext(184, 420, scale=2)
    class C: red = green = blue = 0.92
    if state == "normal": W.draw_meters(cr, 184, 420, C(), [-5.24, -5.25], [-3.1, -3.4], [-2.2, -3.0], [False, False], False, "", 0.014, "validated", 90.0)
    if state == "clip": W.draw_meters(cr, 184, 420, C(), [-1.2, -34.8], [-0.01, -30.0], [-0.01, -29.0], [True, False], False, "", 3.62, "raw", 90.0)
    if state == "stale": W.draw_meters(cr, 184, 420, C(), [-120, -120], [-120, -120], [-120, -120], [False, False], True, "Событие потока: провал несущей L 88 мс — данные не показываются", None, "none", 90.0)
    imgs.append(cr.img.convert("RGB"))
sheet = Image.new("RGB", (184*2*3 + 40, 420*2), (36, 36, 36))
for i, im in enumerate(imgs): sheet.paste(im, (i*(184*2 + 20), 0))
sheet.save(os.path.join(HERE, "preview-meters.png"))
print("saved")
