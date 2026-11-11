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


def plot_margins(right_y_axis: bool = False):
    """Drawing margins in logical pixels; reserve a label column on the right when requested."""
    return 58.0, (48.0 if right_y_axis else 14.0), 30.0, 38.0


class Plot(Gtk.DrawingArea):
    def __init__(self, xlog=True, xlabel="Гц", ylabel="дБ", right_y_axis=False):
        super().__init__()
        self.xlog, self.xlabel, self.ylabel = xlog, xlabel, ylabel
        self.right_y_axis = bool(right_y_axis)
        self.xlim, self.ylim = (20.0, 20000.0), (-100.0, 0.0)
        self.traces = []
        self._mouse = None
        # A manual Y range must survive redraws and new measurements until the
        # user explicitly chooses Auto. This is important for sub-dB response plots.
        self.manual_ylim = None
        self._view_changed_callback = None
        # 0.1.20: надстройки поверх графика — маркеры (номера гармоник) и произвольная панель
        self.markers = []
        self.overlay_draw = None
        self.underlay_draw = None     # рисуется после сетки, ПОД кривыми (например столбцы RTA)
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
    def set_view_changed_callback(self, callback):
        self._view_changed_callback = callback
        if callback is not None:
            callback(self.ylim)

    def _notify_view_changed(self):
        if self._view_changed_callback is not None:
            self._view_changed_callback(self.ylim)

    def set_manual_ylim(self, lo, hi):
        lo, hi = float(lo), float(hi)
        if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
            raise ValueError("Нижняя граница Y должна быть меньше верхней")
        self.manual_ylim = (lo, hi)
        self.ylim = self.manual_ylim
        self._notify_view_changed()
        self.queue_draw()

    def clear_manual_ylim(self):
        self.manual_ylim = None
        self.autoscale_y()

    def set_view(self, xlim=None, ylim=None, xlabel=None, ylabel=None):
        changed = False
        y_changed = False
        if xlim:
            value = (float(xlim[0]), float(xlim[1]))
            if value != self.xlim:
                self.xlim = value
                changed = True
        if ylim and self.manual_ylim is None:
            value = (float(ylim[0]), float(ylim[1]))
            if value != self.ylim:
                self.ylim = value
                changed = True
                y_changed = True
        if xlabel is not None and xlabel != self.xlabel:
            self.xlabel = xlabel
            changed = True
        if ylabel is not None and ylabel != self.ylabel:
            self.ylabel = ylabel
            changed = True
        if y_changed:
            self._notify_view_changed()
        if changed:
            self.queue_draw()

    def set_traces(self, traces):
        """traces: [{'x','y','label','color':(r,g,b)|'L'|'R'|'ref','dash':bool,'width'}]"""
        self.traces = []
        for t in traces:
            x = np.asarray(t["x"], dtype=float)
            y = np.asarray(t["y"], dtype=float).copy()
            # Exact zero is important in deviation plots: remove floating-point
            # residue from subtraction/interpolation before rendering or cursor labels.
            y[np.abs(y) < 1e-8] = 0.0
            ok = np.isfinite(x) & np.isfinite(y)
            if self.xlog:
                ok &= x > 0
            c = t.get("color", "L")
            self.traces.append({"x": x[ok], "y": y[ok], "label": t.get("label", ""),
                                "color": COLORS[c] if isinstance(c, str) else c,
                                "dash": t.get("dash", False), "width": t.get("width", 1.6),
                                "marker": t.get("marker", False),
                                "alpha": float(t.get("alpha", 1.0)),
                                # 'max' — огибающая максимумов (для линий/тонов);
                                # 'mean' — среднее по мощности (для шумовой плотности:
                                # максимум по сотням бинов на столбец завышает полку на ~7–10 дБ)
                                "decim": t.get("decim", "max"), "_dec": {}})
        self.queue_draw()

    def set_markers(self, markers):
        """markers: [{'x','y','text','color':(r,g,b)|None}] — подписи над точками графика."""
        self.markers = list(markers or [])
        self.queue_draw()

    def set_underlay(self, draw_fn):
        """draw_fn(cr, left, top, width, height, fg) — рисунок под кривыми (внутри области графика)."""
        self.underlay_draw = draw_fn
        self.queue_draw()

    def x_to_px(self, x, left, width):
        return self._tx(float(x), left, width)

    def y_to_px(self, v, top, height):
        ylo, yhi = self.ylim
        return top + (yhi - v) / (yhi - ylo) * height

    def set_overlay(self, draw_fn):
        """draw_fn(cr, left, top, width, height, fg) рисует панель поверх графика."""
        self.overlay_draw = draw_fn
        self.queue_draw()

    def autoscale_y(self, pad=0.08, min_span=1.0):
        if self.manual_ylim is not None:
            return
        ys = np.concatenate([t["y"] for t in self.traces if t["y"].size] or [np.array([0.0, 1.0])])
        lo, hi = float(np.min(ys)), float(np.max(ys))
        span = max(hi - lo, min_span)
        self.ylim = (lo - span * pad, hi + span * pad)
        self._notify_view_changed()
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
        ml, mr, mt, mb = plot_margins(self.right_y_axis)
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
        first_tick = math.ceil(ylo / step - 1e-10)
        last_tick = math.floor(yhi / step + 1e-10)
        decimals = max(0, min(6, int(math.ceil(-math.log10(step))) if step < 1 else 0))
        for tick_index in range(first_tick, last_tick + 1):
            # Calculate each tick from its integer index; repeated float addition
            # used to turn the mathematical zero into labels such as 7.5e-16.
            v = round(tick_index * step, decimals + 2)
            if abs(v) < max(1e-12, abs(step) * 1e-9):
                v = 0.0
            y = ty(v)
            rgba(0.13)
            cr.move_to(ml, y); cr.line_to(ml + pw, y); cr.stroke()
            rgba(0.75)
            lab = "0" if v == 0.0 else (f"{v:.{decimals}f}" if decimals else f"{v:.0f}")
            ext = cr.text_extents(lab)
            cr.move_to(ml - 6 - ext.x_advance, y + fs * 0.35); cr.show_text(lab)
            if self.right_y_axis:
                cr.move_to(ml + pw + 9.0, y + fs * 0.35)
                cr.show_text(lab)
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
        # Draw all major grid lines. On a logarithmic spectrum ending at a
        # non-decade frequency (for example 96 kHz), explicitly label the
        # right boundary so the Nyquist limit is visible to the user.
        endpoint_label = None
        endpoint_left = None
        if self.xlog:
            endpoint = self.xlim[1]
            endpoint_label = _fmt_f(endpoint)
            endpoint_width = cr.text_extents(endpoint_label).x_advance
            endpoint_left = ml + pw - endpoint_width
        for x, lab in labs:
            px = self._tx(x, ml, pw)
            rgba(0.13)
            cr.move_to(px, mt); cr.line_to(px, mt + ph); cr.stroke()
            rgba(0.75)
            ext = cr.text_extents(lab)
            label_left = px - ext.x_advance / 2
            label_right = label_left + ext.x_advance
            # Avoid printing e.g. "50k 96k" on top of itself at the far edge.
            overlaps_endpoint = (endpoint_left is not None and
                                 label_right >= endpoint_left - 4 and
                                 label_left <= ml + pw)
            is_endpoint = self.xlog and math.isclose(
                x, self.xlim[1], rel_tol=1e-9, abs_tol=1e-9)
            if not overlaps_endpoint or is_endpoint:
                cr.move_to(px - ext.x_advance / 2, mt + ph + fs + 4)
                cr.show_text(lab)
        if endpoint_label is not None and not any(
                math.isclose(x, self.xlim[1], rel_tol=1e-9, abs_tol=1e-9)
                for x, _ in labs):
            rgba(0.75)
            ext = cr.text_extents(endpoint_label)
            cr.move_to(ml + pw - ext.x_advance, mt + ph + fs + 4)
            cr.show_text(endpoint_label)
        rgba(0.9)
        cr.rectangle(ml, mt, pw, ph); cr.stroke()
        rgba(0.8)
        cr.move_to(ml + pw - cr.text_extents(self.xlabel).x_advance, H - 6); cr.show_text(self.xlabel)
        cr.move_to(6, 18); cr.show_text(self.ylabel)
        if self.right_y_axis:
            # Mirrored dB scale makes the high-frequency end readable without
            # repeatedly tracking tick labels across the full plot width.
            cr.save()
            cr.translate(W - 5.0, mt + ph / 2.0)
            cr.rotate(-math.pi / 2.0)
            # Mirror the active left-axis quantity: amplitude and PSD modes
            # have different units, even though both axes share the same dB ticks.
            label = self.ylabel
            cr.move_to(-cr.text_extents(label).x_advance / 2.0, 0)
            cr.show_text(label)
            cr.restore()

        # кривые
        cr.save()
        cr.rectangle(ml, mt, pw, ph); cr.clip()
        if self.underlay_draw is not None:
            cr.save()
            try:
                self.underlay_draw(cr, ml, mt, pw, ph, fg)
            finally:
                cr.restore()
        draws = []
        for t in self.traces:
            if t.get("decim") == "both":
                # огибающая максимумов — блёклой подложкой (видны одиночные спуры), среднее по
                # мощности — поверх (честная шумовая полка без завышения на ВЧ)
                draws.append((t, "max", 0.35 * t.get("alpha", 1.0)))
                draws.append((t, "mean", t.get("alpha", 1.0)))
            else:
                draws.append((t, t.get("decim", "max"), t.get("alpha", 1.0)))
        for t, dmode, dalpha in draws:
            x, y = self._decimate_cached(t, int(pw), dmode)
            if x.size < 2:
                continue
            cr.set_source_rgba(*t["color"], dalpha)
            cr.set_line_width(t["width"])
            cr.set_dash([6.0, 4.0] if t["dash"] else [])
            for i in range(x.size):
                px, py = self._tx(x[i], ml, pw), ty(float(np.clip(y[i], ylo - 1e4, yhi + 1e4)))
                (cr.move_to if i == 0 else cr.line_to)(px, py)
            cr.stroke()
            if t.get("marker"):
                # Filled markers keep sparse measurements (e.g. three crosstalk
                # frequencies) visible instead of relying on a thin connecting line.
                cr.set_dash([])
                cr.set_source_rgba(*t["color"], 1.0)
                for xv, yv in zip(x, y):
                    px = self._tx(float(xv), ml, pw)
                    py = ty(float(np.clip(yv, ylo - 1e4, yhi + 1e4)))
                    cr.arc(px, py, 5.8, 0, 2 * math.pi)
                    cr.fill()
        # маркеры (например номера гармоник)
        cr.save()
        cr.rectangle(ml, mt, pw, ph); cr.clip()
        cr.set_font_size(fs - 2)
        for mk in self.markers:
            try:
                mx = self._tx(float(mk["x"]), ml, pw)
                my = ty(float(np.clip(mk["y"], ylo - 1e4, yhi + 1e4)))
            except (ValueError, KeyError):
                continue
            col = mk.get("color") or (fg.red, fg.green, fg.blue)
            cr.set_source_rgba(*col, 0.95)
            cr.move_to(mx, my - 3); cr.line_to(mx - 3, my - 9); cr.line_to(mx + 3, my - 9)
            cr.close_path(); cr.fill()
            lab = str(mk.get("text", ""))
            ext = cr.text_extents(lab)
            cr.move_to(mx - ext.x_advance / 2, my - 12); cr.show_text(lab)
        cr.restore()
        cr.set_font_size(fs)
        cr.restore()
        cr.set_dash([])
        if self.overlay_draw is not None:
            cr.save()
            try:
                self.overlay_draw(cr, ml, mt, pw, ph, fg)
            finally:
                cr.restore()

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
            unit = self.xlabel.split(",")[-1].strip()
            parts = [f"{xv:.4g} {unit}"]
            for t in self.traces:
                if t["x"].size > 1 and t["x"][0] <= xv <= t["x"][-1] and t["label"]:
                    value = float(np.interp(xv, t['x'], t['y']))
                    if abs(value) < 1e-9:
                        value = 0.0
                    parts.append(f"{t['label']} {value:.1f}")
            rgba(0.95)
            cr.move_to(ml + 8, mt + ph - 8); cr.show_text("   ".join(parts))

    def _decimate_cached(self, t, n, mode=None):
        """Прореживание с кэшем в самой кривой: перерисовка от движения мыши не пересчитывает
        np.unique по 131 тыс. бинов для каждой кривой."""
        mode = mode or t.get("decim", "max")
        key = (int(n), self.xlim, bool(self.xlog), mode)
        cache = t.setdefault("_dec", {})
        hit = cache.get(key)
        if hit is None:
            if len(cache) > 4:
                cache.clear()
            hit = cache[key] = self._decimate(t["x"], t["y"], n, mode)
        return hit

    def _decimate(self, x, y, n, mode="max"):
        if x.size <= 2 * n:
            return x, y
        lo, hi = self.xlim
        pos = ((np.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
               if self.xlog else (x - lo) / (hi - lo))
        px = np.clip((pos * n).astype(int), -1, n + 1)
        _, idx = np.unique(px, return_index=True)
        if mode == "mean":
            cnt = np.diff(np.append(idx, x.size)).astype(float)
            p = np.add.reduceat(np.power(10.0, y / 10.0), idx) / cnt
            return np.add.reduceat(x, idx) / cnt, 10.0 * np.log10(np.maximum(p, 1e-300))
        return x[idx], np.maximum.reduceat(y, idx)
