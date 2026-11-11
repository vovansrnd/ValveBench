"""Виджеты 0.1.20: постоянная полоса уровней справа и панель анализа поверх графика.

Полоса уровней (``MeterStrip``) живёт в окне на всех страницах и не зависит от высоты
страницы: верх шкалы (0 dBFS) и числа всегда видны. Числа L/R и разбег L−R — вверху, над
столбцами, а не под ними, поэтому они не уезжают за нижний край окна.

Рисование — только те примитивы Cairo, что уже используются в проекте.

Текст, рисуемый Cairo («игрушечный» API шрифтов), не должен содержать U+2212 «−»: на целевой
системе в нём нет этого глифа (виден квадрат «L□R»). Вместо него — ASCII «-». Обычные
виджеты GTK (Pango) подставляют шрифт сами, там «−» допустим.
"""
from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

CH_COLORS = ((0.20, 0.52, 0.89), (0.93, 0.45, 0.05))     # L синий, R оранжевый (как на графиках)
RANGES_DB = (60.0, 90.0, 120.0)
DELTA_SPANS = (0.1, 0.25, 0.5, 1.0, 3.0, 10.0)           # ±дБ шкалы разбега, выбирается автоматически
GOOD = (0.22, 0.68, 0.36)
WARN = (0.93, 0.68, 0.14)
BAD = (0.88, 0.25, 0.22)


def pick_delta_span(delta):
    """Наименьшая шкала ±span, на которой |delta| занимает не более 2/3 полуширины."""
    for span in DELTA_SPANS:
        if abs(delta) <= span * 2.0 / 3.0:
            return span
    return DELTA_SPANS[-1]


def fmt_db(v, digits=2):
    """dB с ограничением ширины: от −10 и ниже — на знак меньше, «−0.0» не бывает."""
    if v is None or not math.isfinite(v) or v <= -119.9:
        return "—"
    if abs(v) >= 10.0 and digits > 1:
        digits = 1
    v = round(float(v), digits)
    if v == 0:
        v = 0.0
    return f"{v:.{digits}f}"


class LevelMeters(Gtk.DrawingArea):
    """Два вертикальных RMS-столбца (с Peak Hold и CLIP) и узкий столбец разбега L−R."""

    def __init__(self):
        super().__init__()
        self.rms = [-120.0, -120.0]
        self.peak = [-120.0, -120.0]
        self.hold = [-120.0, -120.0]
        self.clipped = [False, False]
        self.stale = True
        self.stale_text = "аудиопоток закрыт"
        self.delta = None            # дБ, L−R
        self.delta_kind = "none"     # 'validated' | 'raw' | 'none'
        self.range_db = 90.0
        self.volts = [None, None]    # строки «1.23 mV», если вход откалиброван
        self.set_content_width(176)
        self.set_content_height(260)
        self.set_vexpand(True)
        self.set_hexpand(False)
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.add_controller(click)
        self.set_tooltip_text("Клик — сменить шкалу (60 / 90 / 120 дБ). "
                              "Жёлтая черта — Peak Hold, красная полоса вверху — CLIP, красный пунктир — "
                              "предупредительная граница запаса −3 dBFS (цифровое ограничение — около 0 dBFS).")

    # ---- API
    def set_reading(self, c, rms_db, peak_db, clip=False, redraw=True):
        self.stale = False
        self.rms[c], self.peak[c] = float(rms_db), float(peak_db)
        self.hold[c] = max(self.hold[c], self.peak[c])
        self.clipped[c] = self.clipped[c] or bool(clip)
        if redraw:
            self.queue_draw()

    def set_stale(self, text="нет свежих данных АЦП", redraw=True):
        self.stale = True
        self.stale_text = text
        self.delta, self.delta_kind = None, "none"
        self.hold = [-120.0, -120.0]
        if redraw:
            self.queue_draw()

    def set_volts(self, texts, redraw=True):
        self.volts = list(texts)
        if redraw:
            self.queue_draw()

    def set_delta(self, value, kind, redraw=True):
        self.delta = None if value is None else float(value)
        self.delta_kind = kind if value is not None else "none"
        if redraw:
            self.queue_draw()

    def reset_holds(self):
        self.hold = list(self.peak)
        self.clipped = [False, False]
        self.queue_draw()

    def _on_click(self, *_):
        i = RANGES_DB.index(self.range_db) if self.range_db in RANGES_DB else 1
        self.range_db = RANGES_DB[(i + 1) % len(RANGES_DB)]
        self.queue_draw()

    # ---- рисование
    def _draw(self, area, cr, W, H, *_):
        draw_meters(cr, W, H, self.get_color(), self.rms, self.peak, self.hold, self.clipped,
                    self.stale, self.stale_text, self.delta, self.delta_kind, self.range_db,
                    self.volts)


def draw_meters(cr, W, H, fg, rms, peak, hold, clipped, stale, stale_text, delta, delta_kind,
                range_db, volts=None):
    """Чистая функция рисования — отделена от виджета для проверки без GTK."""
    cr.select_font_face("Sans")

    def ink(a):
        cr.set_source_rgba(fg.red, fg.green, fg.blue, a)

    def text(s, x, y, size, a=0.9, align="l", rgb=None, bold=False):
        cr.set_font_size(size)
        if rgb is None:
            ink(a)
        else:
            cr.set_source_rgba(*rgb, a)
        ext = cr.text_extents(s)
        if align == "c":
            x -= ext.x_advance / 2
        elif align == "r":
            x -= ext.x_advance
        cr.move_to(x, y)
        cr.show_text(s)

    pad = 6.0
    scale_w = 30.0
    bar_w = max(30.0, min(52.0, (W - 2 * pad - scale_w - 2 * 8.0 - 28.0) / 2.0))
    dbar_w = 28.0
    x_l = pad + scale_w
    x_d = x_l + bar_w + 8.0
    x_r = x_d + dbar_w + 8.0
    cols = ((x_l, 0), (x_r, 1))

    # ----- шапка: числа уровня (всегда сверху и всегда видны)
    for x0, c in cols:
        cx = x0 + bar_w / 2
        text("LR"[c], cx, 16, 13, 0.95, "c", CH_COLORS[c])
        val = fmt_db(rms[c]) if not stale else "—"
        text(val, cx, 40, 19, 1.0, "c")
        text("dBFS RMS", cx, 53, 9.5, 0.55, "c")
        pk = fmt_db(peak[c], 1) if not stale else "—"
        text(f"pk {pk}", cx, 68, 11, 0.75, "c")
        if volts and volts[c] and not stale:
            text(volts[c], cx, 81, 10.5, 0.85, "c")      # откалиброванное напряжение, если есть
    # разбег L−R — отдельной строкой
    if delta is None or stale:
        text("Δ L-R  —", W / 2, 90, 14, 0.5, "c")
    else:
        text(f"Δ L-R  {delta:+.3f} dB", W / 2, 90, 14, 1.0, "c")
        tag = {"validated": "проверено ΔG", "raw": "по RMS, без проверки"}.get(delta_kind, "")
        if tag:
            text(tag, W / 2, 103, 9.5, 0.6, "c")
    y0, y1 = 114.0, H - 8.0
    track = y1 - y0
    if track < 40:
        return

    def ypos(db):
        return y1 - (max(-range_db, min(0.0, db)) + range_db) / range_db * track

    # ----- шкала и сетка
    step = 20.0 if range_db > 100 else 10.0
    db = 0.0
    while db >= -range_db - 1e-6:
        yy = ypos(db)
        ink(0.20 if (int(db) % 20 == 0) else 0.10)
        cr.set_line_width(1)
        cr.move_to(x_l, yy); cr.line_to(x_r + bar_w, yy); cr.stroke()
        text(f"{int(db)}", x_l - 5, yy + 3.5, 10, 0.8, "r")
        db -= step
    # фон столбцов
    for x0, _c in cols:
        ink(0.07)
        cr.rectangle(x0, y0, bar_w, track)
        cr.fill()

    if stale:
        cr.set_source_rgba(1.0 - fg.red, 1.0 - fg.green, 1.0 - fg.blue, 0.88)
        cr.rectangle(pad, y0 + track / 2 - 28, W - 2 * pad, 76)
        cr.fill()
        ink(0.55)
        cr.set_font_size(11)
        words = stale_text.split(" ")
        line, yy = "", y0 + track / 2 - 10
        for wd in words:
            probe = (line + " " + wd).strip()
            if cr.text_extents(probe).x_advance > W - 2 * pad and line:
                text(line, W / 2, yy, 11, 0.6, "c")
                line, yy = wd, yy + 14
            else:
                line = probe
        text(line, W / 2, yy, 11, 0.6, "c")
        return

    # ----- заполнение, Peak Hold, CLIP
    for x0, c in cols:
        yy = ypos(rms[c])
        cr.set_source_rgba(*CH_COLORS[c], 0.90)
        cr.rectangle(x0 + 2, yy, bar_w - 4, max(0.0, y1 - yy))
        cr.fill()
        hy = ypos(hold[c])
        cr.set_source_rgba(0.95, 0.78, 0.24, 1.0)
        cr.set_line_width(2.5)
        cr.move_to(x0, hy); cr.line_to(x0 + bar_w, hy); cr.stroke()
        if clipped[c]:
            cr.set_source_rgba(*BAD, 1.0)
            cr.rectangle(x0, y0 - 5, bar_w, 5)
            cr.fill()

    # ----- предупредительная граница запаса −3 dBFS (цифровое ограничение начинается около 0 dBFS)
    y3 = ypos(-3.0)
    cr.set_source_rgba(*BAD, 0.85)
    cr.set_line_width(1.2)
    cr.set_dash([4.0, 3.0])
    cr.move_to(x_l, y3)
    cr.line_to(x_r + bar_w, y3)
    cr.stroke()
    cr.set_dash([])
    text("-3", x_l - 5, y3 + 3.5, 9, 0.95, "r", BAD)

    # ----- столбец разбега: вверх — L громче, вниз — R громче
    ymid = (y0 + y1) / 2
    ink(0.07)
    cr.rectangle(x_d, y0, dbar_w, track)
    cr.fill()
    span = pick_delta_span(delta if delta is not None else 0.0)
    for k in (-1.0, -0.5, 0.0, 0.5, 1.0):
        yy = ymid - k * (track / 2)
        ink(0.35 if k == 0.0 else 0.15)
        cr.set_line_width(1.5 if k == 0.0 else 1)
        cr.move_to(x_d, yy); cr.line_to(x_d + dbar_w, yy); cr.stroke()
    text(f"+{span:g}", x_d + dbar_w / 2, y0 + 11, 9, 0.6, "c")
    text(f"-{span:g}", x_d + dbar_w / 2, y1 - 3, 9, 0.6, "c")
    if delta is not None:
        v = max(-span, min(span, delta))
        ymark = ymid - v / span * (track / 2)
        good = abs(delta) <= 0.1
        col = GOOD if good else (WARN if abs(delta) <= 0.5 else BAD)
        alpha = 0.95 if delta_kind == "validated" else 0.55
        cr.set_source_rgba(*col, alpha)
        cr.rectangle(x_d + 4, min(ymid, ymark), dbar_w - 8, abs(ymark - ymid))
        cr.fill()
        cr.set_line_width(3)
        cr.move_to(x_d, ymark); cr.line_to(x_d + dbar_w, ymark); cr.stroke()
    text("L", x_d + dbar_w / 2, y0 + 24, 10, 0.9, "c", CH_COLORS[0])
    text("R", x_d + dbar_w / 2, y1 - 16, 10, 0.9, "c", CH_COLORS[1])


class MeterStrip(Gtk.Box):
    """Правая полоса окна: заголовок, столбцы уровней, плашка состояния потока, сброс."""

    LEVELS = {"ok": "success", "info": "warning", "bad": "error"}

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
        self.set_size_request(184, -1)
        title = Gtk.Label(label="УРОВНИ АЦП", xalign=0.5)
        title.add_css_class("caption-heading")
        self.append(title)
        self.meters = LevelMeters()
        self.append(self.meters)
        self.health = Gtk.Label(label="Поток: закрыт", xalign=0.5, wrap=False,
                                justify=Gtk.Justification.CENTER)
        self.health.add_css_class("caption")
        # Status text may change from a short OK to a long XRUN explanation.
        # It must not resize the permanent meter column; full detail is a tooltip.
        self.health.set_width_chars(22)
        self.health.set_max_width_chars(22)
        self.health.set_single_line_mode(True)
        self.health.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.append(self.health)
        self._health_level = None
        reset = Gtk.Button(label="Сброс Peak / CLIP")
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: self.meters.reset_holds())
        self.append(reset)

    def set_health(self, level, text):
        """level: 'ok' | 'info' | 'bad' | None (нейтрально)."""
        if self._health_level in self.LEVELS:
            self.health.remove_css_class(self.LEVELS[self._health_level])
        if level in self.LEVELS:
            self.health.add_css_class(self.LEVELS[level])
        self._health_level = level
        self.health.set_text(text)
        self.health.set_tooltip_text(text)


# ------------------------------------------------------------------ панель анализа (как в REW)


def _fmt_pct(p, bound=False):
    if p < 0.001:
        s = f"{p:.5f}"
    elif p < 1.0:
        s = f"{p:.4f}"
    else:
        s = f"{p:.3f}" if p < 10.0 else f"{p:.2f}"
    return ("≤" if bound else "") + s + "%"


def format_analysis_rows(res_l, res_r, nharm=9):
    """Строки панели анализа по результатам livemetrics.analyze_live_tone для L и R.

    Возвращает [{'label', 'cells': [L, R], 'kind': 'head'|'main'|'harm', 'dim': [bool, bool]}].
    Значения, не поднявшиеся над локальным шумовым полом на 10 дБ, помечены «≤» (оценка
    сверху) — как credible/bound в dsp.analyze_tone."""
    rows = []
    results = (res_l, res_r)

    def cell(fn):
        out = []
        for r in results:
            if not r or not r.get("ok"):
                out.append("—")
            else:
                out.append(fn(r))
        return out

    def dim(fn):
        return [not (r and r.get("ok")) or bool(fn(r)) for r in results]

    rows.append({"label": "", "cells": ["L", "R"], "kind": "head", "dim": [False, False]})
    rows.append({"label": "f₀, Гц", "cells": cell(lambda r: f"{r['f0']:.2f}"), "kind": "main",
                 "dim": dim(lambda r: False)})
    rows.append({"label": "Уровень, dBFS", "cells": cell(lambda r: f"{r['level_dbfs']:.2f}"),
                 "kind": "main", "dim": dim(lambda r: False)})
    def thd_cell(r):
        if r.get("thd_na"):
            return "н/д (H2>полосы)"
        bound = r["thd_bound"]
        pct = r["thd_upper_pct"] if bound else r["thd_pct"]
        db = r["thd_upper_db"] if bound else r["thd_db"]
        mark = "~" if r.get("thd_partial") else ""
        return f"{mark}{_fmt_pct(pct, bound)} {db:.1f}дБ"
    rows.append({"label": f"THD H2–H{nharm}", "cells": cell(thd_cell), "kind": "main",
                 "dim": dim(lambda r: r["thd_bound"] or r.get("thd_na"))})
    if any(r and r.get("ok") and r.get("thd_partial") for r in results):
        # часть гармоник не поднялась над шумом: показать оценку и верхнюю границу
        rows.append({"label": "THD верх. гр.",
                     "cells": [f"≤{_fmt_pct(r['thd_upper_pct'])} {r['thd_upper_db']:.1f}дБ"
                               if (r and r.get("ok") and r.get("thd_partial")) else "—" for r in results],
                     "kind": "main", "dim": [True, True]})
    rows.append({"label": "THD+N 20–20к",
                 "cells": cell(lambda r: f"{_fmt_pct(r['thdn_pct'])} {r['thdn_db']:.1f}дБ"),
                 "kind": "main", "dim": dim(lambda r: False)})
    rows.append({"label": "N 20–20к, dBFS", "cells": cell(lambda r: f"{r['noise_dbfs']:.1f}"),
                 "kind": "main", "dim": dim(lambda r: False)})
    rows.append({"label": "N (A), dBFS", "cells": cell(lambda r: f"{r['noise_a_dbfs']:.1f}"),
                 "kind": "main", "dim": dim(lambda r: False)})
    rows.append({"label": "SINAD, дБ", "cells": cell(lambda r: f"{r['sinad_db']:.1f}"),
                 "kind": "main", "dim": dim(lambda r: False)})
    rows.append({"label": "ENOB, бит", "cells": cell(lambda r: f"{r['enob']:.1f}"),
                 "kind": "main", "dim": dim(lambda r: False)})
    rows.append({"label": "DR (FS-N), дБ", "cells": cell(lambda r: f"{r['dr_db']:.1f}"),
                 "kind": "main", "dim": dim(lambda r: False)})
    for h in range(2, int(nharm) + 1):
        def pick(r, h=h):
            for x in r["harmonics"]:
                if x["h"] == h:
                    return x
            return None

        def txt(r, h=h):
            x = pick(r)
            if x is None:
                return "—"
            return f"{'≤' if not x['credible'] else ''}{x['dbc']:.1f} {_fmt_pct(x['pct'])}"
        rows.append({"label": f"H{h}, dBc", "cells": cell(txt), "kind": "harm",
                     "dim": dim(lambda r, h=h: (pick(r) is not None and not pick(r)["credible"]))})
    return rows


def draw_analysis_card(cr, left, top, width, height, fg, rows, subtitle="", compact=False, side="left"):
    """Полупрозрачная панель с таблицей L | R в верхнем углу графика (side: left | right)."""
    cr.select_font_face("Monospace")
    fs = 11.5
    cr.set_font_size(fs)
    line = fs + 4.0
    lab_w, col_w, pad = 118.0, 128.0, 9.0
    shown = [r for r in rows if not (compact and r["kind"] == "harm")]
    card_w = pad * 2 + lab_w + 2 * col_w
    card_h = pad * 2 + line * (len(shown) + (1 if subtitle else 0))
    x0 = (left + width - card_w - 10.0) if side == "right" else (left + 10.0)
    y0 = top + 10.0
    if card_h > height - 20.0 or card_w > width - 20.0:
        return
    cr.set_source_rgba(1.0 - fg.red, 1.0 - fg.green, 1.0 - fg.blue, 0.86)
    cr.rectangle(x0, y0, card_w, card_h)
    cr.fill()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
    cr.set_line_width(1)
    cr.rectangle(x0, y0, card_w, card_h)
    cr.stroke()
    y = y0 + pad + fs
    if subtitle:
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.65)
        cr.move_to(x0 + pad, y)
        cr.show_text(subtitle)
        y += line
    for r in shown:
        if r["kind"] == "head":
            cr.set_source_rgba(*CH_COLORS[0], 1.0); cr.move_to(x0 + pad + lab_w, y); cr.show_text("L")
            cr.set_source_rgba(*CH_COLORS[1], 1.0); cr.move_to(x0 + pad + lab_w + col_w, y); cr.show_text("R")
            y += line
            continue
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.65)
        cr.move_to(x0 + pad, y)
        cr.show_text(r["label"])
        for i in (0, 1):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55 if r["dim"][i] else 1.0)
            cr.move_to(x0 + pad + lab_w + i * col_w, y)
            cr.show_text(r["cells"][i])
        y += line
