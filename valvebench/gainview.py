"""Крупный индикатор ΔG (L−R) для подбора ламп и каналов.

Только Cairo-примитивы, уже используемые в ui.py (прямоугольники, линии, текст).
Состояние приходит из gainmatch.GainTracker; здесь нет вычислений."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .gainmatch import classify, pick_range  # noqa: E402

COLORS = {"ok": (0.22, 0.68, 0.36), "near": (0.93, 0.68, 0.14), "far": (0.88, 0.25, 0.22),
          "off": (0.55, 0.55, 0.55)}


class GainDeltaView(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.state = {"valid": False, "msg": "Подай тестовый сигнал L + R"}
        self.tol = 0.10
        self.set_content_width(520)
        self.set_content_height(200)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_state(self, state, tol):
        self.state, self.tol = state, float(tol)
        self.queue_draw()

    def _draw(self, area, cr, w, h, *_):
        fg = self.get_color()
        st = self.state
        cr.select_font_face("Sans")

        def ink(a):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, a)

        def text(s, x, y, size, a=0.9, align="l"):
            cr.set_font_size(size)
            ink(a)
            ext = cr.text_extents(s)
            if align == "c":
                x -= ext.x_advance / 2
            elif align == "r":
                x -= ext.x_advance
            cr.move_to(x, y)
            cr.show_text(s)

        pad = 14.0
        if not st.get("valid"):
            text("ΔG  L − R", pad, 30, 15, 0.7)
            text("— — —", w / 2, h * 0.58, min(h * 0.34, 54), 0.35, "c")
            text(st.get("msg", ""), w / 2, h - 16, 13, 0.7, "c")
            return

        delta, filt, tol = st["delta"], st["filt"], self.tol
        kind = classify(filt, tol)
        col = COLORS[kind]

        # шапка: подпись и режим
        calibrated = bool(st.get("calibrated"))
        text(("ΔG" if calibrated else "ΔADC") + "  L − R", pad, 26, 15, 0.8)
        mode = "по калибровке L/R" if calibrated else "уровни АЦП без поправки петли"
        if abs(st.get("tare_db", 0.0)) > 1e-9:
            mode += f" · ноль сдвинут на {st['tare_db']:+.3f} дБ"
        text(mode, w - pad, 26, 12, 0.6, "r")

        # главное число
        big = min(h * 0.27, 56)
        base = 30 + big                      # базовая линия главного числа
        text(f"{filt:+.3f} dB", pad, base, big, 1.0)
        if kind == "ok":
            verdict = "каналы совпадают" if calibrated else "уровни АЦП совпадают"
        else:
            side = "левый" if filt > 0 else "правый"
            what = "сильнее" if calibrated else "уровень АЦП выше"
            verdict = f"{side} {what} на {abs(filt):.3f} dB"
        cr.set_source_rgba(*col, 1.0)
        cr.set_font_size(16)
        cr.move_to(pad, base + 26)
        cr.show_text(verdict)

        # справа: стабильность, уровни, фаза
        sig = st.get("sigma")
        lines = [f"разброс {'%.4f dB' % sig if sig is not None else '…'} · 3 с",
                 f"L {st['level_dbfs'][0]:.2f} · R {st['level_dbfs'][1]:.2f} dBFS",
                 f"SINAD {min(st['sinad_db']):.0f} dB · ΔΦ {st['phase_deg']:+.2f}°"]
        if abs(st.get("freq_offset_hz", 0.0)) > 0.001:
            lines.append(f"частота трекинга {st['freq_est_hz']:.3f} Гц "
                         f"(смещение {st['freq_offset_hz']:+.3f} Гц)")
        if st.get("gain_db"):
            lines.insert(1, f"G(L) {st['gain_db'][0]:+.2f} · G(R) {st['gain_db'][1]:+.2f} dB")
        y = 52
        for s in lines:
            text(s, w - pad, y, 13, 0.75, "r")
            y += 19

        # шкала
        half = pick_range(filt, tol)
        x0, x1 = pad, w - pad
        yb = h - 36
        xm = (x0 + x1) / 2

        def xp(v):
            return xm + max(-half, min(half, v)) / half * (x1 - x0) / 2

        ink(0.10)
        cr.rectangle(x0, yb - 6, x1 - x0, 12)
        cr.fill()
        cr.set_source_rgba(*COLORS["ok"], 0.45)               # допуск
        cr.rectangle(xp(-tol), yb - 6, xp(tol) - xp(-tol), 12)
        cr.fill()
        for i in range(-4, 5):
            v = half * i / 4
            ink(0.55 if i else 0.9)
            cr.set_line_width(1.5 if i == 0 else 1.0)
            cr.move_to(xp(v), yb - 10)
            cr.line_to(xp(v), yb + 10)
            cr.stroke()
            if i % 2 == 0:
                text(f"{v:+.3g}" if i else "0", xp(v), yb + 24, 11, 0.7, "c")
        # мгновенное значение — точка, сглаженное — стрелка
        ink(0.55)
        cr.rectangle(xp(delta) - 2, yb - 10, 4, 20)
        cr.fill()
        cr.set_source_rgba(*col, 1.0)
        cr.move_to(xp(filt), yb - 8)
        cr.line_to(xp(filt) - 9, yb - 26)
        cr.line_to(xp(filt) + 9, yb - 26)
        cr.close_path()
        cr.fill()
