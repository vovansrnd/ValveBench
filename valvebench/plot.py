"""График на Cairo: лог/лин ось X, много кривых, «призрак» эталона, курсор.

Рисуется векторно, поэтому на 4K и при любом масштабе GTK остаётся чётким.
Кривые прореживаются до ширины окна (огибающая max), так что спектры из
50 000 точек не тормозят."""
from __future__ import annotations

import math

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

COLORS = {"L": (0.20, 0.52, 0.89), "R": (0.93, 0.45, 0.05), "ref": (0.55, 0.55, 0.55)}


def _nice_step(span, target=10):
    """Readable grid interval; include 2.5 so a tall spectrum gets enough labels."""
    raw = max(float(span) / max(1, int(target)), 1e-12)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if raw <= step:
            return step
    return 10 * mag


def _fmt_f(f):
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


class Plot(Gtk.DrawingArea):
    def __init__(self, xlog=True, xlabel="Гц", ylabel="дБ"):
        super().__init__()
        self.xlog, self.xlabel, self.ylabel = xlog, xlabel, ylabel
        self.xlim, self.ylim = (20.0, 20000.0), (-100.0, 0.0)
        self.traces = []
        self._mouse = None
        self.set_content_width(520)
        self.set_content_height(320)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        mc = Gtk.EventControllerMotion()
        mc.connect("motion", self._on_motion)
        mc.connect("leave", self._on_leave)
        self.add_controller(mc)

    # ---- API
    def set_view(self, xlim=None, ylim=None, xlabel=None, ylabel=None):
        if xlim:
            self.xlim = (float(xlim[0]), float(xlim[1]))
        if ylim:
            self.ylim = (float(ylim[0]), float(ylim[1]))
        if xlabel is not None:
            self.xlabel = xlabel
        if ylabel is not None:
            self.ylabel = ylabel
        self.queue_draw()

    def set_traces(self, traces):
        """traces: [{'x','y','label','color':(r,g,b)|'L'|'R'|'ref','dash':bool,'width'}]"""
        self.traces = []
        for t in traces:
            x = np.asarray(t["x"], dtype=float)
            y = np.asarray(t["y"], dtype=float)
            ok = np.isfinite(x) & np.isfinite(y)
            if self.xlog:
                ok &= x > 0
            c = t.get("color", "L")
            self.traces.append({"x": x[ok], "y": y[ok], "label": t.get("label", ""),
                                "color": COLORS[c] if isinstance(c, str) else c,
                                "dash": t.get("dash", False), "width": t.get("width", 1.6)})
        self.queue_draw()

    def autoscale_y(self, pad=0.08, min_span=1.0):
        ys = np.concatenate([t["y"] for t in self.traces if t["y"].size] or [np.array([0.0, 1.0])])
        lo, hi = float(np.min(ys)), float(np.max(ys))
        span = max(hi - lo, min_span)
        self.ylim = (lo - span * pad, hi + span * pad)
        self.queue_draw()

    # ---- мышь
    def _on_motion(self, _c, x, y):
        self._mouse = (x, y)
        self.queue_draw()

    def _on_leave(self, _c):
        self._mouse = None
        self.queue_draw()

    # ---- рисование
    def _tx(self, x, a, w):
        lo, hi = self.xlim
        if self.xlog:
            return a + (math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * w
        return a + (x - lo) / (hi - lo) * w

    def _draw(self, area, cr, W, H, *_):
        fg = self.get_color()
        fs = 13.0
        ml, mr, mt, mb = 58.0, 14.0, 12.0, 38.0
        pw, ph = W - ml - mr, H - mt - mb
        if pw < 50 or ph < 50:
            return
        cr.select_font_face("Sans")
        cr.set_font_size(fs)
        ylo, yhi = self.ylim
        ty = lambda v: mt + (yhi - v) / (yhi - ylo) * ph

        def rgba(a):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, a)

        # сетка и подписи
        cr.set_line_width(1.0)
        # At ordinary desktop sizes use 10 dB subdivisions for a spectrum;
        # the former coarse grid made a 160 dB range show only a few labels.
        step = _nice_step(yhi - ylo, target=max(8, min(24, int(ph / 24))))
        v = math.ceil(ylo / step) * step
        while v <= yhi + 1e-9:
            y = ty(v)
            rgba(0.13)
            cr.move_to(ml, y); cr.line_to(ml + pw, y); cr.stroke()
            rgba(0.75)
            lab = f"{v:g}"
            ext = cr.text_extents(lab)
            cr.move_to(ml - 6 - ext.x_advance, y + fs * 0.35); cr.show_text(lab)
            v += step
        if self.xlog:
            xs = []
            d = 10 ** math.floor(math.log10(self.xlim[0]))
            while d <= self.xlim[1]:
                xs += [d * m for m in (1, 2, 5)]
                d *= 10
            xs = [x for x in xs if self.xlim[0] * 0.999 <= x <= self.xlim[1] * 1.001]
            labs = [(x, _fmt_f(x)) for x in xs]
        else:
            st = _nice_step(self.xlim[1] - self.xlim[0], 8)
            x = math.ceil(self.xlim[0] / st) * st
            labs = []
            while x <= self.xlim[1] + 1e-9:
                labs.append((x, f"{x:g}")); x += st
        for x, lab in labs:
            px = self._tx(x, ml, pw)
            rgba(0.13)
            cr.move_to(px, mt); cr.line_to(px, mt + ph); cr.stroke()
            rgba(0.75)
            ext = cr.text_extents(lab)
            cr.move_to(px - ext.x_advance / 2, mt + ph + fs + 4); cr.show_text(lab)
        rgba(0.9)
        cr.rectangle(ml, mt, pw, ph); cr.stroke()
        rgba(0.8)
        cr.move_to(ml + pw - cr.text_extents(self.xlabel).x_advance, H - 6); cr.show_text(self.xlabel)
        cr.move_to(6, mt + fs); cr.show_text(self.ylabel)

        # кривые
        cr.save()
        cr.rectangle(ml, mt, pw, ph); cr.clip()
        for t in self.traces:
            x, y = self._decimate(t["x"], t["y"], int(pw))
            if x.size < 2:
                continue
            cr.set_source_rgba(*t["color"], 1.0)
            cr.set_line_width(t["width"])
            cr.set_dash([6.0, 4.0] if t["dash"] else [])
            for i in range(x.size):
                px, py = self._tx(x[i], ml, pw), ty(float(np.clip(y[i], ylo - 1e4, yhi + 1e4)))
                (cr.move_to if i == 0 else cr.line_to)(px, py)
            cr.stroke()
        cr.restore()
        cr.set_dash([])

        # легенда
        ly = mt + 16
        for t in self.traces:
            if not t["label"]:
                continue
            cr.set_source_rgba(*t["color"], 1.0)
            cr.set_line_width(2.0)
            cr.set_dash([6.0, 4.0] if t["dash"] else [])
            ext = cr.text_extents(t["label"])
            lx = ml + pw - 12 - ext.x_advance - 26
            cr.move_to(lx, ly - 4); cr.line_to(lx + 20, ly - 4); cr.stroke()
            cr.set_dash([])
            rgba(0.9)
            cr.move_to(lx + 26, ly); cr.show_text(t["label"])
            ly += fs + 4

        # курсор
        if self._mouse and ml <= self._mouse[0] <= ml + pw and mt <= self._mouse[1] <= mt + ph:
            mx = self._mouse[0]
            frac = (mx - ml) / pw
            xv = (10 ** (math.log10(self.xlim[0]) + frac * (math.log10(self.xlim[1]) - math.log10(self.xlim[0])))
                  if self.xlog else self.xlim[0] + frac * (self.xlim[1] - self.xlim[0]))
            rgba(0.35)
            cr.move_to(mx, mt); cr.line_to(mx, mt + ph); cr.stroke()
            parts = [f"{xv:.4g} {self.xlabel}"]
            for t in self.traces:
                if t["x"].size > 1 and t["x"][0] <= xv <= t["x"][-1] and t["label"]:
                    parts.append(f"{t['label']}: {float(np.interp(xv, t['x'], t['y'])):.4g}")
            rgba(0.95)
            cr.move_to(ml + 8, mt + ph - 8); cr.show_text("   ".join(parts))

    def _decimate(self, x, y, n):
        if x.size <= 2 * n:
            return x, y
        lo, hi = self.xlim
        pos = ((np.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
               if self.xlog else (x - lo) / (hi - lo))
        px = np.clip((pos * n).astype(int), -1, n + 1)
        _, idx = np.unique(px, return_index=True)
        return x[idx], np.maximum.reduceat(y, idx)
