"""GTK4 / libadwaita интерфейс. Вся логика измерений — в measure.py; здесь только формы,
графики и фоновые потоки (звук никогда не трогается из потока GUI)."""
from __future__ import annotations

import sys
import threading

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import calib, dsp, measure as m, signals, store, vinyl  # noqa: E402
from .backend import AlsaCli, SimBackend, SimChannel, list_duplex_devices  # noqa: E402
from .plot import Plot  # noqa: E402

RATES = [44100, 48000, 88200, 96000, 176400, 192000]


# ----------------------------------------------------------------------- виджеты


def spin(title, lo, hi, step, val, digits=1, subtitle=None):
    r = Adw.SpinRow.new_with_range(lo, hi, step)
    r.set_title(title)
    r.set_digits(digits)
    r.set_value(val)
    if subtitle:
        r.set_subtitle(subtitle)
    return r


def combo(title, items, sel=0, subtitle=None):
    r = Adw.ComboRow(title=title)
    r.set_model(Gtk.StringList.new(list(items)))
    r.set_selected(sel)
    if subtitle:
        r.set_subtitle(subtitle)
    return r


def group(title, *rows):
    g = Adw.PreferencesGroup(title=title)
    for r in rows:
        g.add(r)
    return g


def button(label, cb, primary=False):
    b = Gtk.Button(label=label)
    if primary:
        b.add_css_class("suggested-action")
        b.add_css_class("pill")
    b.connect("clicked", lambda *_: cb())
    return b


def textview():
    tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                      left_margin=10, top_margin=8, bottom_margin=8)
    sw = Gtk.ScrolledWindow(min_content_height=170)
    sw.set_child(tv)
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    return tv.get_buffer(), sw


class Page(Gtk.Box):
    """Слева параметры, справа график и отчёт."""

    def __init__(self, win, *param_widgets, plot=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=14,
                         margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.win = win
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_size_request(330, -1)
        for w in param_widgets:
            left.append(w)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(left)
        sc.set_size_request(350, -1)
        self.append(sc)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)
        self.plot = plot
        if plot is not None:
            right.append(plot)
        self.buf, sw = textview()
        right.append(sw)
        self.append(right)

    def say(self, txt):
        self.buf.set_text(txt)


# ------------------------------------------------------------------------ страницы


class VerticalMeter(Gtk.DrawingArea):
    """Крупный вертикальный RMS-метр с dBFS-шкалой и Peak Hold."""

    def __init__(self, title, rgb):
        super().__init__()
        self.title = title
        self.rgb = rgb
        self.rms_db = -120.0
        self.peak_db = -120.0
        self.hold_db = -120.0
        self.clipped = False
        self.set_content_width(190)
        self.set_content_height(300)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)

    def set_reading(self, rms_db, peak_db, clipped=False, reset_hold=False):
        self.rms_db = float(rms_db)
        self.peak_db = float(peak_db)
        if reset_hold:
            self.hold_db = self.peak_db
        else:
            self.hold_db = max(self.hold_db, self.peak_db)
        self.clipped = bool(clipped)
        self.queue_draw()

    def reset_hold(self):
        self.hold_db = self.peak_db
        self.queue_draw()

    def _draw(self, area, cr, width, height, *_):
        fg = self.get_color()
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        left, right, top, bottom = 38.0, 12.0, 12.0, 12.0
        x0, x1 = left, width - right
        y0, y1 = top, height - bottom
        track_h = max(1.0, y1 - y0)

        def ypos(db):
            return y1 - (max(-90.0, min(0.0, db)) + 90.0) / 90.0 * track_h

        # фон дорожки
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
        cr.rectangle(x0, y0, x1 - x0, track_h)
        cr.fill()
        # деления и подписи
        for db in range(-90, 1, 10):
            yy = ypos(db)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.22 if db % 20 == 0 else 0.11)
            cr.set_line_width(1)
            cr.move_to(x0, yy); cr.line_to(x1, yy); cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.82)
            label = f"{db}"
            ext = cr.text_extents(label)
            cr.move_to(x0 - 7 - ext.x_advance, yy + 3.5)
            cr.show_text(label)
        # заполнение RMS
        yy = ypos(self.rms_db)
        r, g, b = self.rgb
        cr.set_source_rgba(r, g, b, 0.88)
        cr.rectangle(x0 + 5, yy, max(1, x1 - x0 - 10), max(0, y1 - yy))
        cr.fill()
        # peak hold
        hy = ypos(self.hold_db)
        cr.set_source_rgba(0.95, 0.78, 0.24, 1.0)
        cr.set_line_width(2.5)
        cr.move_to(x0 + 1, hy); cr.line_to(x1 - 1, hy); cr.stroke()
        # clip marker
        if self.clipped:
            cr.set_source_rgba(0.92, 0.18, 0.18, 1.0)
            cr.rectangle(x0, y0, x1 - x0, 5)
            cr.fill()


class LivePage(Gtk.Box):
    """Главный стенд: генератор слева, два крупных вертикальных метра справа."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                         margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.win = win
        self.f = spin("Частота, Гц", 5, 40000, 1, 1000, 0)
        self.lv = spin("Уровень генератора, dBFS", -100, -0.5, 0.5, -24, 1)
        self.route = combo("Канал генератора", ["L + R", "Только L", "Только R"], 0)
        self.on = Adw.SwitchRow(title="Генератор включён")
        self.vlabel = Gtk.Label(xalign=0, wrap=True)
        self.vlabel.add_css_class("dim-label")
        self.meter_l = VerticalMeter("LEFT", (0.18, 0.52, 0.84))
        self.meter_r = VerticalMeter("RIGHT", (0.86, 0.49, 0.16))
        self.read_l = Gtk.Label(label="— dBFS", xalign=0)
        self.read_r = Gtk.Label(label="— dBFS", xalign=0)
        self.read_l.add_css_class("title-2")
        self.read_r.add_css_class("title-2")
        self.detail_l = Gtk.Label(label="RMS — · Peak — · —", xalign=0)
        self.detail_r = Gtk.Label(label="RMS — · Peak — · —", xalign=0)
        self.detail_l.add_css_class("dim-label")
        self.detail_r.add_css_class("dim-label")
        self.status_label = Gtk.Label(label="Остановлен", xalign=0)
        self.status_label.add_css_class("dim-label")
        self.plot = Plot(True, "Частота, Гц", "dBFS / бин")
        self.plot.set_view((10, min(40000, 0.45 * win.rig_fs())), (-140, 0))
        self.avg = None
        self.timer = None

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        controls.set_size_request(300, -1)
        controls.append(group("Генератор", self.f, self.lv, self.route, self.on))
        controls.append(self.vlabel)
        controls.append(button("Сбросить Peak Hold", self.reset_holds))
        controls.append(self.status_label)
        controls.append(Gtk.Label(label="Напряжения показываются только после калибровки АЦП. Для входа фонокорректора необходим внешний аттенюатор и корректное сопротивление источника.",
                                  xalign=0, wrap=True))
        self.append(controls)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_hexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.append(Gtk.Label(label="КАНАЛЫ L / R", xalign=0))
        header.append(Gtk.Box(hexpand=True))
        self.clock_label = Gtk.Label(label="RMS · Peak · Peak Hold", xalign=1)
        self.clock_label.add_css_class("dim-label")
        header.append(self.clock_label)
        right.append(header)

        meters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        meters.set_vexpand(True)
        for name, meter, read, detail in (("LEFT", self.meter_l, self.read_l, self.detail_l),
                                          ("RIGHT", self.meter_r, self.read_r, self.detail_r)):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.set_hexpand(True)
            title = Gtk.Label(label=name)
            title.add_css_class("heading")
            col.append(title)
            col.append(read)
            meter.set_vexpand(True)
            col.append(meter)
            col.append(detail)
            meters.append(col)
        right.append(meters)
        right.append(Gtk.Label(label="Спектр входного сигнала", xalign=0))
        self.plot.set_content_height(210)
        right.append(self.plot)
        self.append(right)

        for w in (self.f, self.lv):
            w.connect("notify::value", lambda *_: self.apply())
        self.route.connect("notify::selected", lambda *_: self.apply())
        self.on.connect("notify::active", lambda *_: self.apply())

    def reset_holds(self):
        self.meter_l.reset_hold()
        self.meter_r.reset_hold()

    def apply(self):
        be = self.win.be
        if be is None or not be.live_running:
            return
        if not self.on.get_active():
            be.live_set_loop(None)
            self.vlabel.set_text("Генератор выключен; вход остаётся активным")
            return
        fs = self.win.rig_fs()
        freq = float(self.f.get_value())
        amp = float(dsp.dbfs_to_amp(self.lv.get_value()))
        # Фаза замыкается точно на периоде; длина петли 1 секунду.
        n = int(fs)  # односекундный буфер; целочисленная частота замыкается без щелчка
        actual_freq = float(round(freq))
        idx = np.arange(n, dtype=np.float64)
        sig = amp * np.sin(2 * np.pi * actual_freq * idx / fs)
        route = self.route.get_selected()
        buf = np.column_stack((sig if route in (0, 1) else np.zeros(n),
                               sig if route in (0, 2) else np.zeros(n)))
        be.live_set_loop(buf)
        vin_l = self.win.cal.dut_in_v(self.lv.get_value(), 0)
        vin_r = self.win.cal.dut_in_v(self.lv.get_value(), 1)
        self.vlabel.set_text(f"Тестовый сигнал: {actual_freq:.3f} Гц (задано {freq:.1f}), {self.lv.get_value():.1f} dBFS"
                             + (f" · Uin L {calib.fmt_v(vin_l)} / R {calib.fmt_v(vin_r)} RMS"
                                if vin_l is not None or vin_r is not None else ""))

    def start(self):
        self.avg = None
        self.timer = GLib.timeout_add(150, self.tick)
        self.apply()

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def tick(self):
        be = self.win.be
        if be is None or not be.live_running:
            self.timer = None
            if self.win.live_btn.get_active():
                reason = getattr(be, "live_error", "") if be is not None else ""
                if reason:
                    self.win.toast(f"Аудиопоток остановился: {reason}")
                self.win.live_btn.set_active(False)  # вызывает гарантированную очистку backend
            return False
        x = be.live_recent(0.7)
        if len(x) < 4096:
            return True
        n = 1 << int(np.floor(np.log2(min(len(x), 65536))))
        traces = []
        readings = []
        for c, (meter, read, detail, name) in enumerate(((self.meter_l, self.read_l, self.detail_l, "L"),
                                                          (self.meter_r, self.read_r, self.detail_r, "R"))):
            seg = x[-n:, c]
            rms = float(np.sqrt(np.mean(seg ** 2)))
            peak = float(np.max(np.abs(seg)))
            d_rms = float(dsp.rms_dbfs(rms))
            d_peak = float(dsp.db20(peak))
            clip = peak >= 0.98
            meter.set_reading(d_rms, d_peak, clip)
            volts = self.win.cal.in_v(d_rms, c)
            gen_db = float(self.lv.get_value())
            selected_route = self.route.get_selected()
            driven = (selected_route == 0 or (selected_route == 1 and c == 0) or
                      (selected_route == 2 and c == 1))
            vin = self.win.cal.dut_in_v(gen_db, c) if self.on.get_active() and driven else None
            gain = self.win.cal.gain_db(gen_db, d_rms, c) if self.on.get_active() and driven else None
            read.set_text(f"{d_rms:7.2f} dBFS")
            detail.set_text(f"Peak {d_peak:.1f} · Hold {meter.hold_db:.1f} dBFS"
                            + (f" · Uout {calib.fmt_v(volts)}" if volts else "")
                            + (f" · G {gain:+.2f} dB" if gain is not None else "")
                            + (f" · Uin {calib.fmt_v(vin)}" if vin else "")
                            + (" · CLIP" if clip else ""))
            blk = dsp.Block(seg, self.win.rig_fs())
            self.avg = self.avg if self.avg is not None and self.avg.shape == (2, blk.amp.size) else np.zeros((2, blk.amp.size)) + 1e-18
            self.avg[c] = 0.72 * self.avg[c] + 0.28 * (blk.amp ** 2)
            traces.append({"x": blk.freq[1:], "y": 10 * np.log10(self.avg[c][1:] + 1e-18),
                           "label": name, "color": name, "width": 1.2})
            readings.append(clip)
        self.plot.set_traces(traces)
        gains = []
        if self.on.get_active() and self.route.get_selected() == 0:
            for c in (0, 1):
                seg = x[-n:, c]
                rr = float(np.sqrt(np.mean(seg ** 2)))
                gd = self.win.cal.gain_db(float(self.lv.get_value()), float(dsp.rms_dbfs(rr)), c)
                if gd is not None:
                    gains.append(gd)
        if len(gains) == 2:
            self.clock_label.set_text(f"ΔG L−R = {gains[0]-gains[1]:+.3f} dB · RMS / Peak / Hold")
        elif self.on.get_active() and self.route.get_selected() != 0:
            self.clock_label.set_text("Для ΔG выбери генератор L + R · одноканальный тест")
        else:
            self.clock_label.set_text("RMS / Peak / Peak Hold · калибровка задаётся в Setup")
        self.status_label.set_text("ВНИМАНИЕ: перегрузка ADC" if any(readings) else "Измерение входа активно")
        return True


class ThdPage(Page):
    def __init__(self, win):
        self.f = spin("Частота, Гц", 10, 40000, 1, 1000, 0)
        self.lv = spin("Уровень генератора, dBFS", -100, -0.5, 0.5, -20, 1)
        self.route = combo("Канал", ["L+R", "L", "R"], 0)
        self.lvls = Adw.EntryRow(title="Уровни для THD(уровень), dBFS")
        self.lvls.set_text("-6, -12, -18, -24, -36, -48")
        plot = Plot(True, "Гц", "dBFS")
        plot.set_view((10, 40000), (-140, 0))
        super().__init__(win, group("Тон", self.f, self.lv, self.route),
                         button("Измерить THD", self.go, True),
                         group("THD от уровня", self.lvls),
                         button("Снять THD(уровень)", self.go_level),
                         plot=plot)

    def go(self):
        f, lv, r = self.f.get_value(), self.lv.get_value(), ("both", "L", "R")[self.route.get_selected()]
        self.win.run_job(lambda rig: m.thd_point(rig, f, lv, r), self.done)

    def done(self, res):
        self.win.last = res
        self.say(m.text_thd(res))
        self.plot.xlog = True
        self.plot.set_view((10, 40000), (-140, 0), ylabel="dBFS (линия, бин 1 Гц)")
        self.plot.set_traces([{"x": r["freq"][1:], "y": r["spec_db"][1:], "label": c, "color": c, "width": 1.0}
                              for c, r in res["ch"].items()])

    def go_level(self):
        try:
            lv = [float(s) for s in self.lvls.get_text().replace(";", ",").split(",") if s.strip()]
        except ValueError:
            self.say("Уровни — числа через запятую")
            return
        f = self.f.get_value()
        self.win.run_job(lambda rig: m.thd_vs_level(rig, f, lv), self.done_level)

    def done_level(self, res):
        self.win.last = res
        self.say(m.text_thd_level(res))
        tr = []
        for c, rows in res["ch"].items():
            xs = [r["gen"] for r in rows]
            tr.append({"x": xs, "y": [r["thd_db"] for r in rows], "label": f"{c} THD", "color": c})
            tr.append({"x": xs, "y": [r["thdn_db"] for r in rows], "label": f"{c} THD+N", "color": c, "dash": True})
        self.plot.xlog = False
        self.plot.set_view((min(xs) - 3, max(xs) + 3), (-120, 0), xlabel="dBFS ген", ylabel="дБ")
        self.plot.set_traces(tr)


class ResponsePage(Page):
    VIEWS = ["Отклонение от RIAA, дБ", "АЧХ (норм. на 1 кГц), дБ", "THD(f), дБ", "THD(f), %", "Фаза, °"]

    def __init__(self, win):
        self.lo = spin("От, Гц", 5, 1000, 1, 20, 0)
        self.hi = spin("До, Гц", 1000, 40000, 100, 20000, 0)
        self.dur = spin("Длительность свипа, с", 3, 30, 1, 8, 0)
        self.lv = spin("Уровень @1 кГц, dBFS", -90, -1, 1, -20, 1)
        self.mode = combo("Генератор", ["inverse-RIAA", "плоский"], 0,
                          "inverse-RIAA: на выходе корректора ровная линия")
        self.iec = combo("Эталон", ["RIAA", "RIAA + IEC"], 0)
        self.view = combo("Показывать", self.VIEWS, 0)
        self.res = None
        self.ref = None
        plot = Plot(True, "Гц", "дБ")
        super().__init__(win, group("Свип (по Фарине)", self.lo, self.hi, self.dur, self.lv, self.mode, self.iec),
                         button("Измерить", self.go, True), group("Вид", self.view),
                         self._buttons(), plot=plot)
        self.view.connect("notify::selected", lambda *_: self.draw())

    def _buttons(self):
        box = Gtk.Box(spacing=8)
        box.append(button("Закрепить как эталон", self.pin))
        box.append(button("Сбросить", self.unpin))
        box.append(button("Сохранить", self.save))
        return box

    def go(self):
        a = dict(f_lo=self.lo.get_value(), f_hi=self.hi.get_value(), dur=self.dur.get_value(),
                 level_1k_dbfs=self.lv.get_value(), mode=("riaa", "flat")[self.mode.get_selected()],
                 iec=self.iec.get_selected() == 1)
        self.win.run_job(lambda rig: m.sweep(rig, **a), self.done)

    def done(self, res):
        self.res = self.win.last = res
        self.say(m.text_sweep(res))
        self.draw()

    def pin(self):
        if self.res:
            self.ref = self.res
            self.draw()

    def unpin(self):
        self.ref = None
        self.draw()

    def save(self):
        if self.res:
            p = store.save_json(self.res)
            store.sweep_csv(self.res, p[:-5] + ".csv")
            self.win.toast(f"Сохранено: {p}")

    def draw(self):
        v = self.view.get_selected()
        key, ylabel, xk = [("riaa_dev_db", "дБ", "freq"), ("fr_norm_db", "дБ", "freq"),
                           ("thd_db", "дБ", "thd_freq"), ("thd_pct", "%", "thd_freq"),
                           ("phase_deg", "°", "freq")][v]
        traces = []
        for src, tag in ((self.ref, "эталон "), (self.res, "")):
            if src is None:
                continue
            for c, r in src["ch"].items():
                traces.append({"x": r[xk], "y": r[key], "label": f"{tag}{c}",
                               "color": "ref" if tag else c, "dash": bool(tag), "width": 1.4})
        if not traces:
            return
        f_lo = (self.res or self.ref)["f_lo"]
        f_hi = (self.res or self.ref)["f_hi"]
        self.plot.set_view((f_lo, f_hi), ylabel=ylabel)
        self.plot.set_traces(traces)
        if v == 0:
            self.plot.set_view(ylim=(-3, 3))
            ys = np.concatenate([t["y"] for t in self.plot.traces])
            if ys.size and np.max(np.abs(ys)) > 3:
                self.plot.autoscale_y()
        elif v == 3:
            self.plot.autoscale_y(min_span=0.001)
        else:
            self.plot.autoscale_y()


class ImdPage(Page):
    def __init__(self, win):
        self.preset = combo("Метод", list(m.IMD_PRESETS), 0)
        self.f1 = spin("f1, Гц", 5, 40000, 10, 19000, 0)
        self.f2 = spin("f2, Гц", 5, 40000, 10, 20000, 0)
        self.mode = combo("Соотношение", ["1:1 (CCIF)", "4:1 (SMPTE/DIN)"], 0)
        self.lv = spin("Пик суммы, dBFS", -100, -0.5, 0.5, -12, 1)
        plot = Plot(True, "Гц", "dBFS (линия)")
        plot.set_view((10, 48000), (-140, 0))
        super().__init__(win, group("Два тона", self.preset, self.f1, self.f2, self.mode, self.lv),
                         button("Измерить IMD", self.go, True), plot=plot)
        self.preset.connect("notify::selected", lambda *_: self.pick())

    def pick(self):
        f1, f2, mode = list(m.IMD_PRESETS.values())[self.preset.get_selected()]
        self.f1.set_value(f1)
        self.f2.set_value(f2)
        self.mode.set_selected(1 if mode == "smpte" else 0)

    def go(self):
        f1, f2, lv = self.f1.get_value(), self.f2.get_value(), self.lv.get_value()
        mode = ("ccif", "smpte")[self.mode.get_selected()]
        self.win.run_job(lambda rig: m.imd(rig, f1, f2, mode, lv), self.done)

    def done(self, res):
        self.win.last = res
        self.say(m.text_imd(res))
        self.plot.set_traces([{"x": r["freq"][1:], "y": r["spec_db"][1:], "label": c, "color": c, "width": 1.0}
                              for c, r in res["ch"].items()])


class NoisePage(Page):
    def __init__(self, win):
        self.sec = spin("Длительность, с", 2, 60, 1, 8, 0, "вход корректора закорочен или нагружен источником")
        plot = Plot(True, "Гц", "dBFS/√Гц")
        plot.set_view((10, 48000), (-160, -60))
        super().__init__(win, group("Шум", self.sec), button("Измерить шум", self.go, True),
                         group("Каналы", Adw.ActionRow(title="Проникновение L↔R: 100 Гц, 1 кГц, 10 кГц")),
                         button("Измерить проникновение", self.go_x), plot=plot)

    def go(self):
        s = self.sec.get_value()
        self.win.run_job(lambda rig: m.noise(rig, s), self.done)

    def done(self, res):
        self.win.last = res
        self.say(m.text_noise(res))
        self.plot.set_traces([{"x": r["freq"][1:], "y": r["dens_dbfs"][1:], "label": c, "color": c, "width": 1.2}
                              for c, r in res["ch"].items()])

    def go_x(self):
        self.win.run_job(lambda rig: m.crosstalk(rig), lambda r: self.say(m.text_crosstalk(r)))


class NoiseResponsePage(Page):
    """Одноразовая и живая АЧХ по известному шуму: H1 transfer + coherence."""

    VIEWS = ["Отклонение от RIAA, дБ", "АЧХ, нормированная на 1 кГц", "Когерентность"]

    def __init__(self, win):
        self.sec = spin("Длительность измерения, с", 4, 60, 1, 8, 0,
                        "Одноразовый прогон; Live использует окно 4–8 с")
        self.kind = combo("Тестовый сигнал", ["Розовый шум", "Белый шум", "Шум inverse-RIAA"], 0)
        self.level = spin("Цифровой RMS-уровень, dBFS", -80, -12, 1, -42, 0,
                          "Пиковый запас автоматически ограничивается до -3 dBFS")
        self.lo = spin("Нижняя частота, Гц", 10, 1000, 1, 20, 0)
        self.hi = spin("Верхняя частота, Гц", 1000, 40000, 100, 20000, 0)
        self.iec = combo("Эталон коррекции", ["RIAA", "RIAA + IEC"], 0)
        self.view = combo("График", self.VIEWS, 0)
        self.plot = Plot(True, "Частота, Гц", "Отклонение, дБ")
        self.live_toggle = Gtk.ToggleButton.new_with_label("Живая АЧХ")
        self.live_toggle.add_css_class("suggested-action")
        self.live_hint = Gtk.Label(label="Live подаёт непрерывный периодический шум через ЦАП и обновляет H1 по входу АЦП.",
                                   xalign=0, wrap=True)
        self.live_hint.add_css_class("dim-label")
        live_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        live_box.append(self.live_toggle)
        live_box.append(self.live_hint)
        self.live_box = live_box
        super().__init__(win, group("Шумовая передаточная характеристика H1", self.sec, self.kind,
                                    self.level, self.lo, self.hi, self.iec),
                         button("Измерить шумовую АЧХ", self.go, True), live_box,
                         group("Отображение", self.view),
                         Gtk.Label(label=("Это не просто спектр выхода: программа знает сигнал ЦАП и оценивает "
                                          "H1=Sxy/Sxx. Когерентность показывает достоверность частотных точек. "
                                          "В результат входят ЦАП, аттенюатор, DUT и АЦП; без петлевого профиля "
                                          "это относительное измерение."), xalign=0, wrap=True),
                         plot=self.plot)
        self.view.connect("notify::selected", lambda *_: self.draw())
        self.live_toggle.connect("toggled", self.on_live_toggle)
        self.res = None
        self.live_period = None
        self.live_info = None
        self.live_timer = None
        self.live_busy = False
        self.live_sequence = 0

    def _settings(self):
        if self.hi.get_value() <= self.lo.get_value():
            raise ValueError("Верхняя частота должна быть выше нижней")
        fs = self.win.rig_fs()
        if self.hi.get_value() >= 0.45 * fs:
            raise ValueError(f"Верхняя частота должна быть ниже 0.45·Fs ({0.45 * fs:.0f} Гц)")
        kind = ("pink", "white", "riaa")[self.kind.get_selected()]
        iec = self.iec.get_selected() == 1
        preemph = (lambda f: 10.0 ** (-dsp.riaa_playback_db(np.maximum(f, 1.0), iec) / 20.0)) if kind == "riaa" else None
        return fs, kind, iec, preemph

    def go(self):
        if self.live_toggle.get_active():
            self.say("Сначала выключи «Живая АЧХ», затем запускай одноразовый прогон.")
            return
        try:
            _, kind, iec, _ = self._settings()
        except Exception as exc:
            self.say(str(exc))
            return
        args = (self.sec.get_value(), kind, self.level.get_value(), self.lo.get_value(),
                self.hi.get_value(), iec)
        self.win.run_job(lambda rig: m.noise_response(rig, *args), self.done, "Шумовая АЧХ…")

    def done(self, res):
        self.res = res
        self.win.last = res
        self.say(m.text_noise_response(res))
        self.draw()

    def draw(self):
        if self.res is None:
            return
        traces = []
        key = ("riaa_dev_db", "fr_norm_db", "coherence")[self.view.get_selected()]
        self.plot.set_view((self.lo.get_value(), self.hi.get_value()),
                           ((0, 1.05) if key == "coherence" else (-40, 40) if key == "fr_norm_db" else (-6, 6)),
                           ylabel="Когерентность" if key == "coherence" else "дБ")
        for ch in ("L", "R"):
            r = self.res["ch"][ch]
            traces.append({"x": r["freq"], "y": r[key], "label": ch, "color": ch, "width": 1.5})
        self.plot.set_traces(traces)

    def on_live_toggle(self, btn):
        if btn.get_active():
            self.start_live()
        else:
            self.stop_live(from_header=False)

    def start_live(self):
        if self.live_timer is not None:
            return
        try:
            fs, kind, iec, preemph = self._settings()
            # Live использует период 4–8 с: его можно держать в 12-секундном
            # кольцевом буфере без роста памяти и задержки интерфейса.
            seconds = min(8.0, max(4.0, float(self.sec.get_value())))
            x, info = signals.make_test_noise(fs, seconds, kind=kind,
                                              rms_dbfs=float(self.level.get_value()),
                                              f_lo=float(self.lo.get_value()), f_hi=float(self.hi.get_value()),
                                              seed=24680, riaa_shape=preemph)
        except Exception as exc:
            self.say(f"Не удалось подготовить тестовый шум: {exc}")
            self.live_toggle.set_active(False)
            return

        # Стартуем общий живой аудиопоток; после запуска заменяем синус LivePage
        # на периодический шумовой буфер.
        if not self.win.live_btn.get_active():
            self.win.live_btn.set_active(True)
        if not self.win.live_on or self.win.be is None or not self.win.be.live_running:
            self.say("Не удалось запустить аудиопоток. Проверь устройство и текст диагностики.")
            self.live_toggle.set_active(False)
            return
        self.live_period = x
        self.live_info = info
        self.live_sequence += 1
        self.win.be.live_set_loop(np.column_stack((x, x)))
        self.live_busy = False
        for control in (self.sec, self.kind, self.level, self.lo, self.hi, self.iec):
            control.set_sensitive(False)
        self.live_hint.set_text(f"LIVE · {kind} · {seconds:.0f} с · {info['actual_rms_dbfs']:.1f} dBFS RMS · ждём заполнения окна")
        self.say("Живая шумовая АЧХ запущена. Накопление окна…")
        self.live_timer = GLib.timeout_add(1100, self.live_tick)

    def live_tick(self):
        if not self.live_toggle.get_active():
            self.live_timer = None
            return False
        be = self.win.be
        if be is None or not be.live_running:
            self.live_timer = None
            self.live_toggle.set_active(False)
            self.live_hint.set_text("Аудиопоток остановлен")
            self.say("Живой режим был остановлен. Нажми «Живая АЧХ», чтобы запустить снова.")
            return False
        if self.live_busy or self.live_period is None:
            return True
        # В кольцевом буфере есть запас до 12 секунд; берём отрезок длиннее
        # одного периода, чтобы найти его задержку корреляцией.
        rec = be.live_recent(max(10.0, len(self.live_period) / self.win.rig_fs() + 1.0))
        if len(rec) < len(self.live_period):
            return True
        self.live_busy = True
        source = self.live_period.copy()
        fs = self.win.rig_fs()
        lo, hi = float(self.lo.get_value()), float(self.hi.get_value())
        iec = self.iec.get_selected() == 1
        sequence = self.live_sequence

        def work():
            try:
                result = m.noise_transfer_capture(source, rec, fs, lo, hi, iec)
                err = None
            except Exception as exc:  # noqa: BLE001
                result, err = None, exc
            GLib.idle_add(self.live_done, sequence, result, err)

        threading.Thread(target=work, daemon=True).start()
        return True

    def live_done(self, sequence, result, err):
        if sequence != self.live_sequence or not self.live_toggle.get_active():
            self.live_busy = False
            return False
        self.live_busy = False
        if err is not None:
            self.live_hint.set_text(f"LIVE · ожидаю устойчивые данные: {err}")
            return False
        self.res = result
        self.win.last = result
        self.draw()
        l, r = result["ch"]["L"], result["ch"]["R"]
        self.live_hint.set_text(
            f"LIVE · {self.live_info['kind']} · {self.live_info['actual_rms_dbfs']:.1f} dBFS · "
            f"coherence ≥0.8: L {100*l['valid_fraction_coh_0_8']:.0f}% / "
            f"R {100*r['valid_fraction_coh_0_8']:.0f}% · задержка {result['alignment_samples']/self.win.rig_fs():.3f} с")
        self.say("Живая H1-АЧХ обновляется.\n" +
                 "Частотные точки с низкой когерентностью нельзя считать достоверными.\n" +
                 f"L: coherence ≥0.8 на {100*l['valid_fraction_coh_0_8']:.1f}% полосы.\n" +
                 f"R: coherence ≥0.8 на {100*r['valid_fraction_coh_0_8']:.1f}% полосы.\n" +
                 "В графике показано выбранное представление.")
        return False

    def stop_live(self, from_header=False):
        if self.live_timer is not None:
            GLib.source_remove(self.live_timer)
            self.live_timer = None
        self.live_sequence += 1
        self.live_period = None
        self.live_busy = False
        for control in (self.sec, self.kind, self.level, self.lo, self.hi, self.iec):
            control.set_sensitive(True)
        be = self.win.be
        if be is not None:
            try:
                be.live_set_loop(None)
                be.live_stop()
            except Exception as exc:  # noqa: BLE001
                self.say(f"Ошибка остановки аудио: {exc}")
        if not from_header and self.win.live_btn.get_active():
            self.win.live_btn.set_active(False)
        self.live_hint.set_text("Живой режим остановлен")


class VinylPage(Page):
    """Анализ WAV-записей дорожек TACET Vinyl: Check."""

    TESTS = ["A2 · 3150 Hz / стабильность скорости", "A3 · Фаза mono / out-of-phase",
             "A4 · Crosstalk, сигнал только L", "A4 · Crosstalk, сигнал только R",
             "A5 · 341 + 659 → 1000 Hz", "A6 · 300 Hz / skating", "A7 · Резонанс 5–15 Hz",
             "B2 · Частотная характеристика (автотрекинг)"]

    def __init__(self, win):
        self.path = Adw.EntryRow(title="Путь к стерео WAV")
        self.path.set_text("")
        self.test = combo("Дорожка TACET", self.TESTS, 0)
        self.start = spin("Начало участка, с", 0, 3600, 0.5, 0, 1)
        self.duration = spin("Длительность участка, с (0 = до конца)", 0, 3600, 1, 0, 0)
        info = Gtk.Label(label=(
            "TACET Vinyl: Check. Эта страница анализирует записанные дорожки, а не управляет проигрывателем. "
            "A1, B1/B4 и часть A6 остаются прежде всего слуховыми тестами. A2 ниже показывает "
            "нестабильность частоты без нормативного weighting-фильтра — это ещё не DIN/IEC Wow & Flutter. "
            "A5 показывает продукт 1000 Гц, но не измеряет угол VTA напрямую. Для A6 нужна запись всей "
            "41-секундной дорожки. Запишите дорожку через выбранный ADC, затем укажите WAV."),
            xalign=0, wrap=True)
        self.plot = Plot(True, "Частота, Гц", "Уровень / отклонение")
        super().__init__(win, group("Запись", self.path, self.test, self.start, self.duration),
                         button("Анализировать WAV", self.go, True), info, plot=self.plot)

    def go(self):
        path = self.path.get_text().strip()
        if not path:
            self.say("Укажи путь к WAV-файлу")
            return
        test = ("3150", "phase", "xtalk-L", "xtalk-R", "sum", "skating", "resonance", "response")[self.test.get_selected()]
        start_s, duration_s = self.start.get_value(), self.duration.get_value()
        self.win.status.set_text("Анализ записи TACET…")
        self.win.spinner.start()

        def work():
            try:
                fs, x = vinyl.read_wav(path)
                a = max(0, int(start_s * fs))
                b = len(x) if duration_s <= 0 else min(len(x), a + int(duration_s * fs))
                part = x[a:b]
                if test == "3150":
                    result = vinyl.analyze_3150_stability(np.mean(part, axis=1), fs)
                elif test == "phase":
                    result = vinyl.analyze_phase(part, fs)
                elif test == "xtalk-L":
                    result = vinyl.analyze_crosstalk(part, fs, active="L")
                elif test == "xtalk-R":
                    result = vinyl.analyze_crosstalk(part, fs, active="R")
                elif test == "sum":
                    result = vinyl.analyze_vta_sum(np.mean(part, axis=1), fs)
                elif test == "response":
                    result = vinyl.analyze_frequency_response(part, fs)
                elif test == "skating":
                    result = vinyl.analyze_skating_300(part, fs)
                else:
                    result = vinyl.analyze_arm_resonance(np.mean(part, axis=1), fs)
                err = None
            except Exception as exc:  # noqa: BLE001
                result, err = None, exc
            GLib.idle_add(self._finish, result, err)

        threading.Thread(target=work, daemon=True).start()

    def _finish(self, result, err):
        self.win.spinner.stop()
        self.win.status.set_text("")
        if err is not None:
            self.say(f"Ошибка анализа WAV:\n{type(err).__name__}: {err}")
            return False
        self.say(vinyl.text_result(result))
        if result["kind"] not in ("wow_flutter_unweighted", "arm_resonance", "skating_300", "frequency_response"):
            self.plot.set_traces([])
        if result["kind"] == "frequency_response":
            rows = result["segments"]
            self.plot.xlog = True
            self.plot.set_view((20, result["band_hi_hz"]), (-40, 15),
                               xlabel="Частота, Гц", ylabel="Уровень относительно 1 кГц, дБ")
            self.plot.set_traces([
                {"x": [q["frequency_hz"] for q in rows], "y": [q["L_db"] for q in rows],
                 "label": "L", "color": "L", "width": 1.5},
                {"x": [q["frequency_hz"] for q in rows], "y": [q["R_db"] for q in rows],
                 "label": "R", "color": "R", "width": 1.5},
            ])
        elif result["kind"] == "wow_flutter_unweighted":
            self.plot.xlog = False
            self.plot.set_view((0, float(result["time_s"][-1])), (-5, 5),
                               xlabel="Время, с", ylabel="Δf, Гц")
            self.plot.set_traces([{"x": result["time_s"], "y": result["frequency_hz"] - result["mean_hz"],
                                   "label": "Δf", "color": "L"}])
        elif result["kind"] == "arm_resonance":
            self.plot.set_view((5, 15), (-160, 0), ylabel="dBFS/√Гц")
            self.plot.set_traces([{"x": result["freq"], "y": result["density_dbfs_hz"],
                                   "label": "5–15 Hz", "color": "L"}])
        elif result["kind"] == "skating_300":
            rows = result["rows"]
            self.plot.xlog = False
            self.plot.set_view((0, rows[-1]["time_s"]), (-120, 0), xlabel="Время, с", ylabel="dBFS / THD %")
            self.plot.set_traces([{"x": [q["time_s"] for q in rows], "y": [q["L_level_dbfs"] for q in rows], "label": "L", "color": "L"},
                                  {"x": [q["time_s"] for q in rows], "y": [q["R_level_dbfs"] for q in rows], "label": "R", "color": "R"}])
        return False


class SetupPage(Page):
    def __init__(self, win):
        c = win.cal
        self.mains = combo("Частота сети", ["50 Гц", "60 Гц"], 0 if c["mains"] == 50 else 1)
        self.band = spin("Верхняя граница THD/шума, Гц", 5000, 40000, 1000, c["band_hi"], 0)
        self.att = spin("Внешний аттенюатор ЦАП → вход DUT, дБ", -90, 0, 1, c["att_db"], 1,
                        "Укажи потери всего внешнего делителя/аттенюатора, например 1:100 = −40 дБ")
        self.out_l = spin("ЦАП L: В RMS при 0 dBFS", 0, 50, 0.01, c["out_fs_vrms"][0] or 0, 3)
        self.out_r = spin("ЦАП R: В RMS при 0 dBFS", 0, 50, 0.01, c["out_fs_vrms"][1] or 0, 3)
        self.in_l = spin("АЦП L: В RMS при 0 dBFS", 0, 50, 0.01, c["in_fs_vrms"][0] or 0, 3)
        self.in_r = spin("АЦП R: В RMS при 0 dBFS", 0, 50, 0.01, c["in_fs_vrms"][1] or 0, 3)
        self.meter_l = spin("Мультиметр: выход DAC L, мВ RMS", 0, 20000, 0.1, 0, 1,
                            "Подать 1 кГц −6 dBFS, измерять непосредственно на выходе DAC")
        self.meter_r = spin("Мультиметр: выход DAC R, мВ RMS", 0, 20000, 0.1, 0, 1)
        super().__init__(win, group("Измерительные параметры", self.mains, self.band, self.att),
                         group("Калибровка ЦАП (независимо L/R)", self.out_l, self.out_r),
                         group("Калибровка АЦП (независимо L/R)", self.in_l, self.in_r),
                         group("Показания мультиметра при −6 dBFS", self.meter_l, self.meter_r),
                         button("Применить показания мультиметра к DAC L/R", self.cal_out),
                         button("Калибровать ADC L/R по петле DAC→ADC", self.cal_in),
                         button("Сохранить настройки", self.apply))

    def apply(self):
        c = self.win.cal
        c["mains"] = 50.0 if self.mains.get_selected() == 0 else 60.0
        c["band_hi"] = self.band.get_value()
        c["att_db"] = self.att.get_value()
        c["out_fs_vrms"] = [self.out_l.get_value() or None, self.out_r.get_value() or None]
        c["in_fs_vrms"] = [self.in_l.get_value() or None, self.in_r.get_value() or None]
        calib.save(c)
        self.win.toast("Настройки сохранены")

    def cal_out(self):
        ml, mr = self.meter_l.get_value(), self.meter_r.get_value()
        if ml <= 0 and mr <= 0:
            self.say("Введи хотя бы одно показание мультиметра")
            return
        if ml > 0:
            self.win.cal.set_out_from_meter(-6.0, ml / 1000.0, 0)
            self.out_l.set_value(self.win.cal["out_fs_vrms"][0])
        if mr > 0:
            self.win.cal.set_out_from_meter(-6.0, mr / 1000.0, 1)
            self.out_r.set_value(self.win.cal["out_fs_vrms"][1])
        self.apply()

    def cal_in(self):
        if not all(self.win.cal["out_fs_vrms"]):
            self.say("Сначала откалибруй оба выхода ЦАП L и R мультиметром")
            return

        def job(rig):
            return m.thd_point(rig, 1000.0, -12.0)

        def done(res):
            for i, channel in enumerate(("L", "R")):
                self.win.cal.set_in_from_loop(-12.0, res["ch"][channel]["level_dbfs"], i)
            self.in_l.set_value(self.win.cal["in_fs_vrms"][0])
            self.in_r.set_value(self.win.cal["in_fs_vrms"][1])
            self.apply()
        self.win.run_job(job, done, "Калибровка ADC по петле…")


# --------------------------------------------------------------------------- окно


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ValveBench", default_width=1500, default_height=940)
        self.cal = calib.load()
        self.be = None
        self.rig = None
        self.busy = False
        self.live_on = False
        self.last = None
        # Измерительный прибор требует одновременно ЦАП и АЦП. Не показываем
        # HDMI playback-only устройства как полноценные дуплексные карты.
        self.cards = [("sim", "Симулятор (демо без карты)")] + list_duplex_devices()
        self.toasts = Adw.ToastOverlay()

        header = Adw.HeaderBar()
        self.dev = Gtk.DropDown.new_from_strings([n for _, n in self.cards])
        saved_device = self.cal["device"]
        sel = next((i for i, (d, _) in enumerate(self.cards) if d == saved_device), 0)
        if self.cards[sel][0] != saved_device:
            self.cal["device"] = "sim"
            self.invalid_saved_device = saved_device
        else:
            self.invalid_saved_device = None
        self.dev.set_selected(sel)
        self.rate = Gtk.DropDown.new_from_strings([f"{r / 1000:g} кГц" for r in RATES])
        self.rate.set_selected(RATES.index(self.cal["fs"]) if self.cal["fs"] in RATES else 3)
        self.live_btn = Gtk.ToggleButton(label="Живой режим")
        self.live_btn.connect("toggled", self.on_live)
        self.status = Gtk.Label(label="", xalign=1)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.dev)
        header.pack_start(self.rate)
        header.pack_start(self.live_btn)
        header.pack_start(button("Такт", self.check_clock))
        header.pack_end(self.spinner)
        header.pack_end(self.status)
        self.dev.connect("notify::selected", self.on_device_changed)
        self.rate.connect("notify::selected", lambda *_: self.reset_rig())

        self.stack = Gtk.Stack()
        self.live = LivePage(self)
        self.noise_response_page = NoiseResponsePage(self)
        for name, title, page in (("live", "Gain Match / Уровень", self.live), ("thd", "THD / THD+N", ThdPage(self)),
                                  ("resp", "АЧХ / RIAA / Sweep", ResponsePage(self)),
                                  ("noise_response", "АЧХ шумом / H1", self.noise_response_page),
                                  ("imd", "IMD", ImdPage(self)), ("noise", "Шум / Crosstalk", NoisePage(self)),
                                  ("vinyl", "TACET / Vinyl Test", VinylPage(self)),
                                  ("setup", "Калибровка / Setup", SetupPage(self))):
            self.stack.add_titled(page, name, title)
        side = Gtk.StackSidebar(stack=self.stack)
        side.set_size_request(190, -1)
        body = Gtk.Box()
        body.append(side)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.stack.set_hexpand(True)
        self.stack.connect("notify::visible-child", self.on_page_changed)
        body.append(self.stack)
        self.toasts.set_child(body)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(self.toasts)
        self.set_content(tv)
        self.connect("close-request", self.on_close)
        if self.invalid_saved_device:
            self.toast(f"Сохранённое устройство {self.invalid_saved_device} не является доступным дуплексным ALSA-устройством. Выбран симулятор; выбери E-MU в списке.")

    def on_device_changed(self, *_):
        dev, _label = self.cards[self.dev.get_selected()]
        self.cal["device"] = dev
        self.reset_rig()

    def on_page_changed(self, *_):
        # Не оставляем тестовый шум подаваемым, если пользователь ушёл с прибора.
        page = self.stack.get_visible_child()
        if page is not self.noise_response_page and self.noise_response_page.live_toggle.get_active():
            self.noise_response_page.live_toggle.set_active(False)

    # ---- стенд
    def rig_fs(self):
        return RATES[self.rate.get_selected()]

    def make_backend(self):
        dev = self.cards[self.dev.get_selected()][0]
        if dev == "sim":
            return SimBackend(SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=True, noise_dbfs=-105,
                                         hum50_dbfs=-105, hum100_dbfs=-112),
                              SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=True, noise_dbfs=-103,
                                         hum50_dbfs=-92, hum100_dbfs=-108))
        self.cal["device"] = dev
        return AlsaCli(dev, in_map=self.cal["in_map"], out_map=self.cal["out_map"])

    def reset_rig(self):
        self.stop_live()
        self.be = self.rig = None
        self.cal["fs"] = self.rig_fs()

    def get_rig(self):
        if self.rig is None:
            self.be = self.make_backend()
            self.rig = m.Rig(self.be, self.cal, fs=self.rig_fs())
            self.rig.open()
            for n in self.be.notes:
                self.toast(n)
        return self.rig

    # ---- живой режим
    def on_live(self, btn):
        if btn.get_active():
            self.start_live()
        else:
            self.stop_live()

    def start_live(self):
        if self.busy:
            self.live_btn.set_active(False)
            return
        try:
            self.get_rig()
            self.be.live_start()
        except Exception as e:  # noqa: BLE001
            self.toast(f"Не удалось открыть карту: {e}")
            self.live_btn.set_active(False)
            return
        self.live_on = True
        self.live.start()

    def stop_live(self):
        # live_stop у бэкенда идемпотентен и должен очищать процессы даже
        # после того, как один из них завершился неожиданно.
        self.live.stop()
        if self.be is not None:
            self.be.live_stop()
        self.live_on = False
        if self.live_btn.get_active():
            self.live_btn.set_active(False)

    # ---- фоновые задачи
    def run_job(self, fn, done, msg="Измерение…"):
        if self.busy:
            self.toast("Подожди окончания текущего измерения")
            return
        resume = self.live_on
        if resume:
            self.stop_live()
        try:
            rig = self.get_rig()  # GTK/device selections are read on the UI thread.
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Не удалось открыть аудиоустройство: {exc}")
            if resume:
                self.live_btn.set_active(True)
            return
        self.busy = True
        self.status.set_text(msg)
        self.spinner.start()

        def finish(res, err):
            self.busy = False
            self.spinner.stop()
            self.status.set_text("")
            if err is not None:
                self.toast(f"Ошибка: {err}")
                page = self.stack.get_visible_child()
                if hasattr(page, "say"):
                    page.say(f"Ошибка измерения:\n{err}")
            else:
                done(res)
            if resume:
                self.live_btn.set_active(True)
            return False

        def work():
            try:
                res = fn(rig)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(finish, None, e)
                return
            GLib.idle_add(finish, res, None)

        threading.Thread(target=work, daemon=True).start()

    def check_clock(self):
        self.run_job(m.clock_check, lambda r: (self.stack.set_visible_child_name("setup"),
                                               self.stack.get_visible_child().say(m.text_clock(r))), "Проверка такта…")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    def on_close(self, *_):
        self.stop_live()
        calib.save(self.cal)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.valvebench")
        self.connect("activate", lambda a: Window(a).present())


def main():
    return App().run(sys.argv)
