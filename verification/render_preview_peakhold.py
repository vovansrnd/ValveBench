"""Превью страницы «АЧХ по пластинке»: настоящая логика страницы под заглушкой gi + PIL-имитация cairo."""
import sys, os, math
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import stubgi; stubgi.install()
import tempfile; os.environ["HOME"] = tempfile.mkdtemp()
from fakecairo import FakeContext
from valvebench import ui, peakhold as ph
import types

class W:
    live_on = True; be = types.SimpleNamespace(live_events=lambda since=0: [])
    def __init__(s): s.generator_btn = types.SimpleNamespace(get_active=lambda: False)
    def toast(s, m): pass
    def rig_fs(s): return 96000
    def post_health(s, *a, **k): pass
ui.PeakHoldPage.OVERLAYS_PATH = os.path.join(os.environ["HOME"], "o.json")
pp = ui.PeakHoldPage(W())

fs, n = 96000, 32768
def sweep(fr, xt, frac=1.0, seed=1):
    rng = np.random.default_rng(seed); T = 40.0
    t = np.arange(int(T * frac * fs)) / fs; k = 20000 / 20.0
    phase = 2 * np.pi * 20 * T / math.log(k) * (k ** (t / T) - 1); f = 20 * k ** (t / T)
    A = 10 ** (fr(f) / 20)
    L = A * np.sin(phase); R = A * 10 ** (xt(f) / 20) * np.sin(phase + 0.5) + 2e-6 * rng.standard_normal(t.size)
    for pos in rng.integers(0, t.size - 100, 40): L[pos:pos + 3] += 0.03 * rng.choice([-1, 1])
    return np.column_stack([L + 1e-6 * rng.standard_normal(t.size), R])
def run(X):
    pp.acc.reset()
    for s0 in range(0, X.shape[0] - n, int(0.3 * fs)):
        r = ph.rta_job(X[s0:s0 + n], fs, n, 12)
        pp._apply(r, {"kind": "peak", "end": None, "n": n, "fs": fs, "po": 12})
# картридж 1 (красный), картридж 2 (пурпурный), ТЕКУЩЕЕ измерение — до 18 кГц с другой АЧХ
fr1 = lambda f: -20 + 1.5 * np.sin(np.log2(f / 40)) + 2.5 * (f > 10000) * np.log2(np.maximum(f, 10000) / 10000) - 3 * (f < 50)
fr2 = lambda f: -21.3 + 0.8 * np.sin(np.log2(f / 60) + 1) - 4 * (f > 12000) * np.log2(np.maximum(f, 12000) / 12000) - 2 * (f < 45)
xt1 = lambda f: -28 - 6 * np.log2(np.maximum(f, 100) / 1000) * 0.5 - 8 * (f < 100)
xt2 = lambda f: -24 - 5 * np.log2(np.maximum(f, 100) / 1000) * 0.5 - 5 * (f < 100)
pp.name_entry = types.SimpleNamespace(get_text=lambda: "AT120 №2", set_text=lambda t: None)
run(sweep(fr1, xt1, 1.0, 1)); pp.on_save()
pp.name_entry = types.SimpleNamespace(get_text=lambda: "Nagaoka MP100", set_text=lambda t: None)
run(sweep(fr2, xt2, 1.0, 2)); pp.on_save()
run(sweep(lambda f: fr1(f) + 0.8 * np.sin(np.log2(f / 300)), xt1, 0.8, 3))
pp.last_res = ph.rta_job(sweep(fr1, xt1, 0.2, 4)[int(5 * fs):int(5 * fs) + n], fs, n, 12)
from PIL import Image
for name, view in (("preview-peakhold.png", 0), ("preview-peakhold-xtalk.png", 2)):
    pp.view.set_selected(view); pp._redraw()
    cr = FakeContext(1180, 520, scale=2); pp.plot._draw(None, cr, 1180, 520); cr.save_png(os.path.join(HERE, name))
print("saved; overlays:", [o.name for o in pp.overlays])
