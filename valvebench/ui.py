"""GTK4 / libadwaita интерфейс. Вся логика измерений — в measure.py; здесь только формы,
графики и фоновые потоки (звук никогда не трогается из потока GUI)."""
from __future__ import annotations

import sys
import threading
import time
import hashlib

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import calib, dsp, measure as m, signals, store, vinyl, preferences  # noqa: E402
from . import gainmatch  # noqa: E402
from .gainmatch import GainTracker  # noqa: E402
from .gainview import GainDeltaView  # noqa: E402
from .backend import (AlsaCli, SimBackend, SimChannel, list_duplex_devices, supported_duplex_rates,
                      stable_device_identity)  # noqa: E402
from .plot import Plot  # noqa: E402

RATES = [44100, 48000, 88200, 96000, 176400, 192000]


# ----------------------------------------------------------------------- виджеты


def _pref_widget_name(title):
    return "pref_" + hashlib.sha1(str(title).encode("utf-8")).hexdigest()[:12]


def spin(title, lo, hi, step, val, digits=1, subtitle=None):
    r = Adw.SpinRow.new_with_range(lo, hi, step)
    r.set_name(_pref_widget_name(title))
    r.set_title(title)
    r.set_digits(digits)
    r.set_value(val)
    if subtitle:
        r.set_subtitle(subtitle)
    return r


def combo(title, items, sel=0, subtitle=None):
    r = Adw.ComboRow(title=title)
    r.set_name(_pref_widget_name(title))
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


def _spectrum_ylim(traces, top=0.0):
    """Choose a readable spectrum floor from the actual curve and card noise floor."""
    arrays = []
    for trace in traces:
        values = np.asarray(trace.get("y", []), dtype=float)
        values = values[np.isfinite(values)]
        if values.size:
            arrays.append(values)
    if not arrays:
        return -160.0, float(top)
    values = np.concatenate(arrays)
    low = float(np.nanpercentile(values, 5.0)) - 10.0
    # Avoid both a useless fixed −140 dB limit and an absurdly tall plot
    # when a single isolated bin sits near numerical zero.
    low = min(-80.0, max(-240.0, low))
    return low, float(top)


def _noise_floor_trace(profile, channel, fs, nfft, average=1.0, line_bins=True):
    """Convert a saved per-card ADC noise-density profile to the graph's units.

    The same measured density is shared by Spectrum/THD/IMD. Line-bin views add
    the analysis-window ENBW; an averaged time waveform (IMD) reduces random
    noise power by the number of independent blocks.
    """
    if not isinstance(profile, dict):
        return None
    pf = np.asarray(profile.get("frequency_hz", []), dtype=float)
    pd = np.asarray(profile.get("channels", {}).get(channel, {}).get("density_dbfs_sqrt_hz", []), dtype=float)
    if pf.size < 2 or pd.size != pf.size:
        return None
    y = pd.copy()
    if line_bins:
        w = dsp.bh(int(nfft))
        enbw = float(fs) * float(np.sum(w * w)) / max(float(np.sum(w) ** 2), 1e-30)
        enbw = enbw / max(1.0, float(average))
        y += 10.0 * np.log10(max(enbw, 1e-30))
    return {"x": pf, "y": y, "label": f"Шум АЦП {channel}",
            "color": "ref", "dash": True, "width": 1.2}


def textview():
    tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                      left_margin=10, top_margin=8, bottom_margin=8)
    sw = Gtk.ScrolledWindow(min_content_height=170)
    sw.set_child(tv)
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    return tv.get_buffer(), sw


def vertical_page_scroller(child):
    """Make the complete contents of a page vertically reachable on short screens.

    Keep the vertical scrollbar non-overlayed so it remains visible when the
    page is taller than the window. Horizontal scrolling is intentionally off:
    controls should wrap/reflow rather than create a second navigation axis.
    """
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_overlay_scrolling(False)
    scroller.set_kinetic_scrolling(True)
    scroller.set_hexpand(True)
    scroller.set_vexpand(True)
    scroller.set_child(child)
    return scroller


class Page(Gtk.Box):
    """Общая измерительная страница с постоянным статусом и управлением масштабом."""

    def __init__(self, win, *param_widgets, plot=None):
        # The page itself stays the Stack child (so status/job routing and
        # page identity remain unchanged); its entire two-column body scrolls.
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self._job_button_states = None
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14,
                          margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_size_request(330, -1)
        for w in param_widgets:
            left.append(w)
        content.append(left)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_bar.add_css_class("card")
        status_bar.set_margin_bottom(3)
        self.job_spinner = Gtk.Spinner()
        self.job_status = Gtk.Label(
            label="ГОТОВО К ИЗМЕРЕНИЮ · выбери параметры и нажми кнопку слева",
            xalign=0, wrap=True)
        self.job_status.add_css_class("title-4")
        self.job_status.set_hexpand(True)
        status_bar.append(self.job_spinner)
        status_bar.append(self.job_status)
        right.append(status_bar)

        self.plot = plot
        if plot is not None:
            right.append(plot)
            scale_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            scale_bar.set_margin_top(2)
            scale_bar.set_margin_bottom(4)
            scale_bar.append(Gtk.Label(label="Масштаб Y:", xalign=0))
            scale_bar.append(Gtk.Label(label="от", xalign=0))
            self.y_min = Gtk.SpinButton.new_with_range(-300.0, 100.0, 0.1)
            self.y_min.set_name("plot_y_min")
            self.y_min.set_digits(2)
            self.y_min.set_size_request(86, -1)
            self.y_min.set_value(float(plot.ylim[0]))
            scale_bar.append(self.y_min)
            scale_bar.append(Gtk.Label(label="до", xalign=0))
            self.y_max = Gtk.SpinButton.new_with_range(-300.0, 100.0, 0.1)
            self.y_max.set_name("plot_y_max")
            self.y_max.set_digits(2)
            self.y_max.set_size_request(86, -1)
            self.y_max.set_value(float(plot.ylim[1]))
            scale_bar.append(self.y_max)
            scale_bar.append(button("Применить", self.apply_plot_scale))
            scale_bar.append(button("Авто", self.auto_plot_scale))
            self.plot_scale_hint = Gtk.Label(label="", xalign=0)
            self.plot_scale_hint.add_css_class("dim-label")
            scale_bar.append(self.plot_scale_hint)
            scale_bar.set_hexpand(True)
            right.append(scale_bar)
            plot.set_view_changed_callback(self._sync_plot_scale)
        else:
            self.y_min = self.y_max = self.plot_scale_hint = None

        self.buf, sw = textview()
        right.append(sw)
        content.append(right)
        self.page_scroller = vertical_page_scroller(content)
        self.append(self.page_scroller)

    def _sync_plot_scale(self, ylim):
        if self.y_min is None:
            return
        self.y_min.set_value(float(ylim[0]))
        self.y_max.set_value(float(ylim[1]))
        if self.plot.manual_ylim is None:
            self.plot_scale_hint.set_text("авто")
        else:
            self.plot_scale_hint.set_text("ручной масштаб")

    def apply_plot_scale(self):
        if self.plot is None:
            return
        lo, hi = self.y_min.get_value(), self.y_max.get_value()
        try:
            self.plot.set_manual_ylim(lo, hi)
            self.plot_scale_hint.set_text(f"Ручной диапазон {lo:g}…{hi:g}")
            self.win.persist_plot_scale(self)
        except ValueError as exc:
            self.plot_scale_hint.set_text(str(exc))

    def auto_plot_scale(self):
        if self.plot is None:
            return
        self.plot.clear_manual_ylim()
        self.plot_scale_hint.set_text("Автомасштаб")
        self.win.clear_plot_scale_preference(self)

    @staticmethod
    def _walk_widgets(root):
        child = root.get_first_child()
        while child is not None:
            yield child
            yield from Page._walk_widgets(child)
            child = child.get_next_sibling()

    def set_job_busy(self, busy):
        if busy:
            if self._job_button_states is None:
                self._job_button_states = [(w, w.get_sensitive()) for w in self._walk_widgets(self)
                                           if isinstance(w, Gtk.Button)]
                for widget, _state in self._job_button_states:
                    widget.set_sensitive(False)
        elif self._job_button_states is not None:
            for widget, was_sensitive in self._job_button_states:
                widget.set_sensitive(was_sensitive)
            self._job_button_states = None

    def set_job_status(self, state, message, elapsed=None):
        if state == "running":
            self.job_spinner.start()
            self.job_status.set_text(f"ИЗМЕРЕНИЕ ВЫПОЛНЯЕТСЯ · {message}")
        else:
            self.job_spinner.stop()
            if state == "success":
                suffix = f" · {elapsed:.1f} с" if elapsed is not None else ""
                self.job_status.set_text(f"ИЗМЕРЕНИЕ ЗАВЕРШЕНО{suffix} · результат ниже")
            elif state == "error":
                self.job_status.set_text(f"ИЗМЕРЕНИЕ НЕ ВЫПОЛНЕНО · {message}")
            else:
                self.job_status.set_text(message)

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
        self.clipped = self.clipped or bool(clipped)
        self.queue_draw()

    def reset_holds(self):
        self.hold_db = self.peak_db
        self.clipped = False
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
    """Главный стенд: генераторные настройки и два крупных вертикальных метра L/R."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                          margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        self.f = spin("Частота, Гц", 5, 40000, 1, 1000, 0)
        self.lv = spin("Уровень генератора, dBFS", -100, -0.5, 0.5, -6, 1)
        self.route = combo("Канал генератора", ["L + R", "Только L", "Только R"], 0)
        self.vlabel = Gtk.Label(xalign=0, wrap=True)
        self.vlabel.add_css_class("dim-label")
        self.meter_l = VerticalMeter("LEFT", (0.18, 0.52, 0.84))
        self.meter_r = VerticalMeter("RIGHT", (0.86, 0.49, 0.16))
        self.read_l = Gtk.Label(label="— dBFS", xalign=0)
        self.read_r = Gtk.Label(label="— dBFS", xalign=0)
        self.read_l.add_css_class("title-2")
        self.read_r.add_css_class("title-2")
        self.detail_l = Gtk.Label(label="RMS — · Peak —", xalign=0)
        self.detail_r = Gtk.Label(label="RMS — · Peak —", xalign=0)
        self.detail_l.add_css_class("dim-label")
        self.detail_r.add_css_class("dim-label")
        self.status_label = Gtk.Label(label="Аудиотракт остановлен", xalign=0, wrap=True)
        self.status_label.add_css_class("dim-label")
        self.clip_label = Gtk.Label(label="ADC CLIP: —", xalign=0)
        self.xrun_label = Gtk.Label(label="XRUN: —", xalign=0)
        self.clock_label = Gtk.Label(label="RMS · Peak · Hold", xalign=1)
        self.timer = None
        self.last_generator_settings = None
        self.tracker = GainTracker()
        self.delta_view = GainDeltaView()
        self.tol = spin("Допуск разницы L/R, дБ", 0.01, 2.0, 0.01, 0.20, 2, "для итогового заключения Gain Match")
        self._delta_error_shown = False

        # Двухшаговый Gain Match: сначала измеряется собственная L/R-ошибка
        # петли звуковой карты, затем она вычитается из измерения DUT.
        self.series_level = spin("Уровень теста, dBFS", -80.0, -0.5, 0.5, -12.0, 1,
                                 "Один и тот же уровень для петли и усилителя")
        self.series_repeats = spin("Повторов на частоту", 1, 5, 1, 3, 0,
                                   "Три частоты измеряются вперемешку")
        self.series_dut = Adw.EntryRow(title="Устройство / модель")
        self.series_dut.set_name(_pref_widget_name("Устройство / модель"))
        self.series_tubes = Adw.EntryRow(title="Лампы / позиции")
        self.series_tubes.set_name(_pref_widget_name("Лампы / позиции"))
        self.series_notes = Adw.EntryRow(title="Примечание")
        self.series_notes.set_name(_pref_widget_name("Примечание"))
        self.series_reference = None
        self.last_series = None
        self._job_button_states = None
        self.series_job_spinner = Gtk.Spinner()
        self.series_job_status = Gtk.Label(label="ГОТОВО К ИЗМЕРЕНИЮ", xalign=0, wrap=True)
        self.series_job_status.add_css_class("title-4")
        self.series_job_status.set_hexpand(True)
        self.series_result_label = Gtk.Label(
            label="Сначала измерь петлю звуковой карты. Без этого результат усилителя не рассчитывается.",
            xalign=0, yalign=0, wrap=True, selectable=True)
        self.series_result_label.set_hexpand(True)
        self.series_result_label.set_margin_top(8)
        self.series_result_label.set_margin_bottom(8)
        self.series_result_label.add_css_class("title-4")
        self.series_reference_label = Gtk.Label(label="Эталон петли: не создан", xalign=0, wrap=True)
        self.series_reference_label.add_css_class("dim-label")
        self.series_loop_btn = button("1. ПРОВЕРИТЬ КАРТУ  ·  L→L, R→R", self.measure_loopback, True)
        self.series_dut_btn = button("2. ИЗМЕРИТЬ УСИЛИТЕЛЬ", self.measure_dut_series, True)
        self.series_dut_btn.set_sensitive(False)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        controls.set_size_request(310, -1)
        # 0.1.18: expose the generator controls; they were created and connected
        # to apply(), but had accidentally been omitted from the LivePage layout.
        controls.append(group("Генератор", self.f, self.lv, self.route))
        controls.append(group("Параметры Gain Match", self.series_level, self.series_repeats, self.tol))
        controls.append(self.series_loop_btn)
        controls.append(self.series_reference_label)
        controls.append(self.series_dut_btn)
        controls.append(group("Что проверяем", self.series_dut, self.series_tubes, self.series_notes))
        controls.append(Gtk.Label(
            label="Шаг 1: соединить выходы карты напрямую со входами (L→L, R→R). "
                  "Шаг 2: вместо петли подключить усилитель. Между шагами не менять "
                  "громкость карты, уровень теста и аттенюаторы. Поправка компенсирует "
                  "карту, но не несовпадение внешних L/R кабелей и делителей. После "
                  "переподключения карты или изменения микшера повторить шаг 1.",
            xalign=0, wrap=True))
        # Единственная кнопка генератора находится на главном измерительном экране.
        # Это тот же ToggleButton, состояние которого управляет аудиодвижком.
        self.gen_action = self.win.generator_btn
        self.gen_action.set_size_request(-1, 60)
        self.gen_action.set_hexpand(True)
        self.gen_action.add_css_class("suggested-action")
        controls.append(self.gen_action)
        hint = Gtk.Label(
            label="Порядок работы: 1) открой ЦАП/АЦП в верхней панели; "
                  "2) нажми эту кнопку. Повторное нажатие выключает сигнал, но оставляет АЦП включённым.",
            xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        controls.append(hint)
        controls.append(self.vlabel)
        controls.append(button("Сбросить Peak Hold и CLIP", self.reset_holds))
        controls.append(button("ΔG: принять текущую разность за ноль", self.tare_delta))
        controls.append(button("ΔG: сбросить ноль", self.clear_tare))
        controls.append(self.status_label)
        controls.append(self.clip_label)
        controls.append(self.xrun_label)
        controls.append(Gtk.Label(
            label="ADC CLIP — отсчёты АЦП приблизились к цифровому пределу. Это не гарантирует отсутствия "
                  "перегрузки аналогового входного каскада. XRUN — underrun ЦАП или overrun АЦП: аудиопоток "
                  "не успел обслужить буфер, возможны пропуски/разрывы.",
            xalign=0, wrap=True))
        controls.append(Gtk.Label(
            label="Баланс усиления L/R измеряется в двух шагах выше и не требует вольтметра. "
                  "Абсолютные Uin, Uout и G каждого канала требуют калибровки напряжения. "
                  "Для входа MM необходим внешний аттенюатор и корректное сопротивление источника.",
            xalign=0, wrap=True))
        content.append(controls)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_hexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(label="ДВА КАНАЛА · ОДИН МАСШТАБ", xalign=0)
        title.add_css_class("heading")
        header.append(title)
        header.append(Gtk.Box(hexpand=True))
        self.clock_label.add_css_class("dim-label")
        header.append(self.clock_label)
        right.append(header)
        right.append(self.delta_view)
        series_status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        series_status.add_css_class("card")
        series_status.append(self.series_job_spinner)
        series_status.append(self.series_job_status)
        right.append(series_status)
        series_scroller = Gtk.ScrolledWindow()
        series_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        series_scroller.set_min_content_height(150)
        series_scroller.set_max_content_height(220)
        series_scroller.set_child(self.series_result_label)
        right.append(series_scroller)

        meters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=26)
        meters.set_vexpand(True)
        for name, meter, read, detail in (("LEFT", self.meter_l, self.read_l, self.detail_l),
                                          ("RIGHT", self.meter_r, self.read_r, self.detail_r)):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            col.set_hexpand(True)
            title = Gtk.Label(label=name)
            title.add_css_class("title-1")
            col.append(title)
            col.append(read)
            meter.set_vexpand(True)
            col.append(meter)
            col.append(detail)
            meters.append(col)
        right.append(meters)
        note = Gtk.Label(label="Для спектра открой отдельный прибор слева. Там крупный график, "
                               "шумовая плотность dBFS/√Гц, линейчатый спектр dBFS/bin и выбор FFT.",
                         xalign=0, wrap=True)
        note.add_css_class("dim-label")
        right.append(note)
        content.append(right)
        self.page_scroller = vertical_page_scroller(content)
        self.append(self.page_scroller)

        for w in (self.f, self.lv):
            w.connect("notify::value", lambda *_: self.apply())
        self.route.connect("notify::selected", lambda *_: self.apply())
        self.series_level.connect("notify::value", lambda *_: self.refresh_gain_reference())
        self.refresh_gain_reference()

    def set_job_busy(self, busy):
        if busy:
            if self._job_button_states is None:
                self._job_button_states = [(w, w.get_sensitive()) for w in Page._walk_widgets(self)
                                           if isinstance(w, Gtk.Button)]
                for widget, _state in self._job_button_states:
                    widget.set_sensitive(False)
        elif self._job_button_states is not None:
            for widget, was_sensitive in self._job_button_states:
                widget.set_sensitive(was_sensitive)
            self._job_button_states = None

    def set_job_status(self, state, message, elapsed=None):
        if state == "running":
            self.series_job_spinner.start()
            self.series_job_status.set_text(f"ИЗМЕРЕНИЕ ВЫПОЛНЯЕТСЯ · {message}")
        else:
            self.series_job_spinner.stop()
            if state == "success":
                suffix = f" · {elapsed:.1f} с" if elapsed is not None else ""
                self.series_job_status.set_text(f"ИЗМЕРЕНИЕ ЗАВЕРШЕНО{suffix} · результат ниже")
            elif state == "error":
                self.series_job_status.set_text(f"ИЗМЕРЕНИЕ НЕ ВЫПОЛНЕНО · {message}")
            else:
                self.series_job_status.set_text(message)

    def refresh_gain_reference(self):
        """Invalidate the in-session reference when card, rate or drive level changes.

        A saved file is kept for traceability, but never silently reused after app
        restart: USB mixer defaults can change on reconnect.
        """
        hw = self.win.cards[self.win.dev.get_selected()][0]
        key = gainmatch.device_key_from_hw(hw)
        level = float(self.series_level.get_value())
        ref = self.series_reference
        matches = bool(ref and ref.get("device_key") == key
                       and int(ref.get("fs", -1)) == int(self.win.rig_fs())
                       and abs(float(ref.get("generator_level_dbfs", 999.0)) - level) < 1e-6)
        if not matches:
            self.series_reference = None
            self.series_reference_label.set_text("Сначала проверь измерительную карту (шаг 1)")
            self.series_dut_btn.set_sensitive(False)
        else:
            self.series_reference_label.set_text(
                f"КОРРЕКЦИЯ КАРТЫ ГОТОВА · {ref['fs']} Гц · можно подключать усилитель")
            self.series_dut_btn.set_sensitive(True)

    def _series_metadata(self):
        return {key: value for key, value in {
            "dut": self.series_dut.get_text().strip(),
            "tubes": self.series_tubes.get_text().strip(),
            "notes": self.series_notes.get_text().strip(),
        }.items() if value}

    def measure_loopback(self):
        """Step 1: characterize the soundcard's L/R loop imbalance per frequency."""
        self.series_reference = None
        self.series_dut_btn.set_sensitive(False)
        self.series_reference_label.set_text("Эталон петли: измеряю…")
        level = float(self.series_level.get_value())
        repeats = int(self.series_repeats.get_value())

        def job(rig):
            return gainmatch.measure_series(rig, level_dbfs=level, repeats=repeats, purpose="loopback")

        def done(result):
            try:
                reference = gainmatch.build_loopback_reference(result)
                path = store.save_gain_reference(reference)
            except (ValueError, OSError):
                self.series_reference = None
                self.series_dut_btn.set_sensitive(False)
                self.refresh_gain_reference()
                raise
            self.series_reference = reference
            self.series_dut_btn.set_sensitive(True)
            self.series_reference_label.set_text(
                f"КОРРЕКЦИЯ КАРТЫ ГОТОВА · {reference['fs']} Гц · можно подключать усилитель")
            valid_rows = sum(1 for row in result.get("rows", [])
                             if row.get("valid") and row.get("repeat_stable"))
            self.series_result_label.set_text(
                "Проверка измерительной карты завершена.\n"
                f"Надёжные частотные точки: {valid_rows} из {len(result.get('rows', []))}.\n"
                "Карта сохранена как эталон. Теперь вместо петли подключи проверяемый усилитель "
                "и нажми «2. ИЗМЕРИТЬ УСИЛИТЕЛЬ».")
            self.win.toast("Карта проверена. Можно переходить к измерению усилителя.")

        self.win.run_job(job, done, "Шаг 1/2: измеряю собственную разницу каналов карты…")

    def measure_dut_series(self):
        """Step 2: measure DUT and subtract the stored per-frequency loopback error."""
        level = float(self.series_level.get_value())
        repeats = int(self.series_repeats.get_value())
        reference = self.series_reference
        if reference is None:
            self.refresh_gain_reference()
            reference = self.series_reference
        if reference is None:
            self.win.toast("Сначала измерь прямую петлю звуковой карты — шаг 1")
            return
        metadata = self._series_metadata()

        def job(rig):
            return gainmatch.measure_series(rig, level_dbfs=level, repeats=repeats,
                                            metadata=metadata, purpose="dut", reference=reference)

        def done(result):
            self.last_series = result
            self.series_result_label.set_text(gainmatch.text_series(result, self.tol.get_value()))
            try:
                json_path, csv_path = store.save_gain_series(result)
                self.win.toast(f"Gain Match сохранён: {json_path} · CSV: {csv_path}")
            except OSError as exc:
                self.win.toast(f"Измерено, но не удалось сохранить результат: {exc}")

        self.win.run_job(job, done, "Шаг 2/2: измеряю усилитель и компенсирую карту…")

    def reset_holds(self):
        self.meter_l.reset_holds()
        self.meter_r.reset_holds()

    def tare_delta(self):
        """Принять текущую ΔG за ноль: подай один и тот же сигнал в заведомо одинаковые каналы
        (петля ЦАП→АЦП или эталонная лампа), нажми — и далее показывается разница относительно них."""
        if self.tracker.last_raw is None:
            self.win.toast("ΔG: пока нет измерения — подай тест в L + R")
            return
        self.tracker.tare()
        self.win.toast(f"ΔG: ноль сдвинут на {self.tracker.tare_db:+.3f} дБ")

    def clear_tare(self):
        self.tracker.clear_tare()
        self.win.toast("ΔG: ноль сброшен")

    def _update_delta(self):
        """Обновить индикатор ΔG. Сбой здесь не должен останавливать основные метры."""
        try:
            be, fs = self.win.be, self.win.rig_fs()
            if not self.generator_active() or self.last_generator_settings is None:
                self.delta_view.set_state({"valid": False, "msg": "Подай тестовый сигнал L + R"},
                                          self.tol.get_value())
                return
            freq, gen_db, route = self.last_generator_settings
            state = self.tracker.update(be.live_recent(0.6), fs, freq, gen_db, route,
                                        self.win.cal, now=GLib.get_monotonic_time() / 1e6)
            self.delta_view.set_state(state, self.tol.get_value())
        except Exception as exc:  # noqa: BLE001
            if not self._delta_error_shown:
                self._delta_error_shown = True
                self.win.toast(f"Индикатор ΔG отключён: {type(exc).__name__}: {exc}")

    def sync_generator_button(self, active, enabled):
        self.gen_action.set_sensitive(bool(enabled))
        if active:
            self.gen_action.set_label("■  ОСТАНОВИТЬ ТЕСТОВЫЙ СИГНАЛ")
            self.gen_action.remove_css_class("suggested-action")
            self.gen_action.add_css_class("destructive-action")
        else:
            self.gen_action.set_label("▶  ПОДАТЬ ТЕСТОВЫЙ СИГНАЛ")
            self.gen_action.remove_css_class("destructive-action")
            self.gen_action.add_css_class("suggested-action")

    def generator_active(self):
        return bool(self.win.generator_btn.get_active())

    def apply(self):
        be = self.win.be
        self.tracker.reset()
        if be is None or not be.live_running:
            return
        if not self.generator_active():
            be.live_set_loop(None)
            self.vlabel.set_text("Генератор выключен · ЦАП подаёт тишину · АЦП продолжает измерение")
            self.last_generator_settings = None
            if hasattr(self.win, "spectrum"):
                self.win.spectrum.reset_average()
            return
        fs = self.win.rig_fs()
        freq = float(self.f.get_value())
        amp = float(dsp.dbfs_to_amp(self.lv.get_value()))
        # Используем целое число периодов в секунде — буфер зацикливается без фазового скачка.
        actual_freq = float(round(freq))
        n = int(fs)
        idx = np.arange(n, dtype=np.float64)
        sig = amp * np.sin(2 * np.pi * actual_freq * idx / fs)
        route = self.route.get_selected()
        buf = np.column_stack((sig if route in (0, 1) else np.zeros(n),
                               sig if route in (0, 2) else np.zeros(n)))
        be.live_set_loop(buf)
        self.last_generator_settings = (actual_freq, float(self.lv.get_value()), int(route))
        vin_l = self.win.cal.dut_in_v(self.lv.get_value(), 0)
        vin_r = self.win.cal.dut_in_v(self.lv.get_value(), 1)
        self.vlabel.set_text(
            f"ПОДАНО: {actual_freq:.0f} Гц · {self.lv.get_value():.1f} dBFS · "
            f"{'L + R' if route == 0 else 'только L' if route == 1 else 'только R'}"
            + (f" · расчёт Uin L {calib.fmt_v(vin_l)} / R {calib.fmt_v(vin_r)} RMS"
               if vin_l is not None or vin_r is not None else ""))
        if hasattr(self.win, "spectrum"):
            self.win.spectrum.reset_average()

    def start(self):
        self.timer = GLib.timeout_add(75, self.tick)
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
                self.win.live_btn.set_active(False)
            return False
        # Метр использует короткое окно 100 мс: атака уровня не ждёт полсекундный FFT.
        x = be.live_recent(0.12)
        if len(x) < max(1024, int(0.04 * self.win.rig_fs())):
            return True
        clips = []
        for c, (meter, read, detail) in enumerate(((self.meter_l, self.read_l, self.detail_l),
                                                    (self.meter_r, self.read_r, self.detail_r))):
            seg = x[:, c]
            rms = float(np.sqrt(np.mean(seg ** 2)))
            peak = float(np.max(np.abs(seg)))
            d_rms = float(dsp.rms_dbfs(rms))
            d_peak = float(dsp.db20(peak))
            clip = peak >= 0.98
            meter.set_reading(d_rms, d_peak, clip)
            clips.append(meter.clipped)
            volts = self.win.cal.in_v(d_rms, c)
            gen_db = float(self.lv.get_value())
            route = self.route.get_selected()
            driven = route == 0 or (route == 1 and c == 0) or (route == 2 and c == 1)
            gain = self.win.cal.gain_db(gen_db, d_rms, c) if self.generator_active() and driven else None
            detail.set_text(
                f"Peak {d_peak:.1f} dBFS · Hold {meter.hold_db:.1f} dBFS · запас {max(0.0, -d_peak):.1f} dB"
                + (f" · Uout {calib.fmt_v(volts)}" if volts is not None else "")
                + (f" · G {gain:+.2f} dB" if gain is not None else ""))
            read.set_text(f"{d_rms:7.2f} dBFS RMS")
        status = be.live_status() if hasattr(be, "live_status") else {}
        xrun = status.get("xruns")
        if xrun is None:
            self.xrun_label.set_text("XRUN: backend не сообщает счётчик")
        else:
            self.xrun_label.set_text(
                f"XRUN: {xrun} · ADC overrun {status.get('capture_xruns', 0)} / "
                f"DAC underrun {status.get('playback_xruns', 0)} (сообщения ALSA)" +
                (" · пропуски не зарегистрированы" if xrun == 0 else " · проверь разрывы в сигнале"))
        if any(clips):
            self.clip_label.set_text("ADC CLIP: ДА · измерение могло быть искажено")
            self.clip_label.add_css_class("error")
        else:
            self.clip_label.set_text("ADC CLIP: нет · цифрового клиппинга в текущем окне не обнаружено")
            self.clip_label.remove_css_class("error")
        cap_state = "OK" if status.get("capture_alive", False) else "ОСТАНОВЛЕН"
        play_state = "OK" if status.get("playback_alive", False) else "ОСТАНОВЛЕН"
        self.status_label.set_text(
            f"Аудио активно · Fs={self.win.rig_fs()} Гц · ADC {cap_state} "
            f"({status.get('capture_frames', 0) / self.win.rig_fs():.1f} с принято) · "
            f"DAC {play_state} ({status.get('playback_frames', 0) / self.win.rig_fs():.1f} с отправлено)")
        gains = []
        if self.generator_active() and self.route.get_selected() == 0:
            for c in (0, 1):
                gd = self.win.cal.gain_db(float(self.lv.get_value()),
                                          float(dsp.rms_dbfs(np.sqrt(np.mean(x[:, c] ** 2)))), c)
                if gd is not None:
                    gains.append(gd)
        if len(gains) == 2:
            self.clock_label.set_text(f"ΔG L−R = {gains[0] - gains[1]:+.3f} dB · калибровка L/R")
        elif self.generator_active() and self.route.get_selected() != 0:
            self.clock_label.set_text("ΔG недоступна: тест подан только в один канал")
        else:
            self.clock_label.set_text("RMS / Peak / Peak Hold · входные уровни dBFS")
        self._update_delta()
        return True


class SpectrumPage(Gtk.Box):
    """Большой живой FFT-экран с управляемым диапазоном и быстрой атакой."""

    FFT_SIZES = [8192, 16384, 32768, 65536, 131072, 262144]

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_top=10, margin_bottom=10,
                          margin_start=12, margin_end=12)
        self.win = win
        self.fft = Gtk.DropDown.new_from_strings([
            "8k · самый быстрый отклик",
            "16k · быстро",
            "32k · среднее разрешение",
            "64k · среднее разрешение",
            "128k · высокое разрешение",
            "256k · максимальное разрешение (рекомендуется)"])
        self.fft.set_name("pref_spectrum_fft")
        self.fft.set_selected(5)
        self.mode = Gtk.DropDown.new_from_strings([
            "Амплитуда линий · dBFS/bin", "Плотность шума · dBFS/√Гц"])
        # PSD view is the most useful default for seeing the continuous noise floor.
        self.mode.set_name("pref_spectrum_mode")
        self.mode.set_selected(1)
        self.floor = Gtk.DropDown.new_from_strings(["−120", "−140", "−160", "−180", "−200"])
        self.floor.set_name("pref_spectrum_floor")
        self.floor.set_selected(3)
        self.top = Gtk.DropDown.new_from_strings(["0", "−20", "−40", "−60", "−80"])
        self.top.set_name("pref_spectrum_top")
        self.top.set_selected(0)
        self.band = Gtk.DropDown.new_from_strings([
            "20 Гц – 20 кГц",
            "10 Гц – 40 кГц",
            "10 Гц – Fs/2 (весь спектр до Найквиста)",
        ])
        # В основном режиме показываем весь доступный положительный спектр.
        self.band.set_name("pref_spectrum_band")
        self.band.set_selected(2)
        self.avg_mode = Gtk.DropDown.new_from_strings(["Без усреднения", "Быстрое", "Сглаженное"])
        self.avg_mode.set_name("pref_spectrum_avg")
        self.avg_mode.set_selected(1)
        self.state = Gtk.Label(label="Аудиопоток выключен", xalign=0)
        self.state.add_css_class("dim-label")
        self.plot = Plot(True, "Частота, Гц", "Уровень, dBFS/bin", right_y_axis=True)
        self.plot.set_content_height(540)
        self.plot.set_vexpand(True)
        self.plot.set_hexpand(True)
        self.avg_line_power = None
        self.avg_bin_power = None
        self.timer = None
        self._last_signature = None
        self._last_compute_time = 0.0

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        for label, widget in (("FFT", self.fft), ("Режим отображения", self.mode),
                              ("Низ шкалы, дБ", self.floor), ("Верх шкалы, дБ", self.top),
                              ("Полоса", self.band), ("Усреднение", self.avg_mode)):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            lab = Gtk.Label(label=label, xalign=0)
            lab.add_css_class("dim-label")
            col.append(lab)
            widget.set_hexpand(True)
            col.append(widget)
            toolbar.append(col)
        content.append(toolbar)
        content.append(self.plot)
        content.append(self.state)
        help_label = Gtk.Label(
            label="Амплитуда линий: dBFS/bin, уровень синусоидальной составляющей в каждом бине. "
                  "Плотность шума: dBFS/√Гц, позволяет сравнивать шумовую полку при разном FFT. "
                  "Δf=Fs/N; ENBW окна Блэкмана–Харриса около 2·Δf. Уровень тона и плотность шума — разные величины. "
                  "Режим Fs/2 показывает последний положительный FFT-бин (частоту Найквиста). "
                  "Выше слышимой полосы АЧХ аналогового тракта может быть не нормирована.",
            xalign=0, wrap=True)
        help_label.add_css_class("dim-label")
        content.append(help_label)
        self.page_scroller = vertical_page_scroller(content)
        self.append(self.page_scroller)
        for w in (self.fft, self.mode, self.floor, self.top, self.band, self.avg_mode):
            w.connect("notify::selected", lambda *_: self.reset_average())

    def fft_n(self):
        return self.FFT_SIZES[self.fft.get_selected()]

    def reset_average(self):
        self.avg_line_power = None
        self.avg_bin_power = None
        self._last_signature = None
        self._last_compute_time = 0.0

    def set_visible(self, visible):
        if visible and self.win.live_on and self.timer is None:
            self.timer = GLib.timeout_add(100, self.tick)
        elif not visible and self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None

    def tick(self):
        be = self.win.be
        if be is None or not be.live_running:
            self.timer = None
            self.state.set_text("Аудиопоток выключен · включи «Аудио ВКЛ» в верхней панели")
            return False
        fs = self.win.rig_fs()
        n = self.fft_n()
        refresh_ms = {8192: 80, 16384: 100, 32768: 120, 65536: 180, 131072: 300, 262144: 500}[n]
        now = time.monotonic()
        if self._last_compute_time and (now - self._last_compute_time) * 1000 < refresh_ms:
            return True
        x = be.live_recent(n / fs + 0.02)
        if len(x) < n:
            self.state.set_text(f"Накопление FFT: {len(x)}/{n} отсчётов · {len(x)/fs:.3f} с")
            return True
        x = x[-n:]
        self._last_compute_time = now
        traces = []
        attack = (1.0, 0.78, 0.48)[self.avg_mode.get_selected()]
        release = (1.0, 0.22, 0.12)[self.avg_mode.get_selected()]
        first_frame = self.avg_line_power is None or self.avg_bin_power is None
        blocks = [dsp.Block(x[:, ch], fs) for ch in (0, 1)]
        expected_shape = (2, len(blocks[0].amp))
        if (first_frame or self.avg_line_power.shape != expected_shape or
                self.avg_bin_power.shape != expected_shape):
            self.avg_line_power = np.stack([np.maximum(b.amp ** 2, 1e-30) for b in blocks])
            self.avg_bin_power = np.stack([np.maximum(b.P, 1e-30) for b in blocks])
        for ch, label in ((0, "L"), (1, "R")):
            block = blocks[ch]
            line_power = np.maximum(block.amp ** 2, 1e-30)
            bin_power = np.maximum(block.P, 1e-30)
            if not first_frame:
                prev_line = self.avg_line_power[ch]
                prev_bin = self.avg_bin_power[ch]
                coeff_line = np.where(line_power >= prev_line, attack, release)
                # PSD/noise density must not have attack/release bias: the asymmetric
                # envelope detector elevated the apparent continuous noise floor.
                coeff_bin = 0.5 * (attack + release)
                self.avg_line_power[ch] = coeff_line * line_power + (1.0 - coeff_line) * prev_line
                self.avg_bin_power[ch] = coeff_bin * bin_power + (1.0 - coeff_bin) * prev_bin
            if self.mode.get_selected() == 0:
                # Block.amp is the peak amplitude of a sine-equivalent spectral line.
                y = 10.0 * np.log10(np.maximum(self.avg_line_power[ch], 1e-30))
                ylabel = "Уровень синусоидальной линии, dBFS/bin"
            else:
                # One-sided power per bin -> amplitude spectral density, referenced to full-scale sine RMS.
                df_bin = fs / n
                y = 10.0 * np.log10(np.maximum(2.0 * self.avg_bin_power[ch] / df_bin, 1e-30))
                ylabel = "Плотность шума, dBFS/sqrt(Гц)"
            traces.append({"x": block.freq[1:], "y": y[1:], "label": label, "color": label, "width": 1.7})
        self.plot.xlog = True
        xlo = 20.0 if self.band.get_selected() == 0 else 10.0
        ylo = float([-120, -140, -160, -180, -200][self.floor.get_selected()])
        yhi = float([0, -20, -40, -60, -80][self.top.get_selected()])
        if ylo >= yhi:
            ylo, yhi = (-160.0, 0.0) if self.mode.get_selected() == 0 else (-200.0, -60.0)
        band_index = self.band.get_selected()
        xlo, xhi = dsp.spectrum_view_limits(fs, band_index)
        if band_index == 0:
            band_name = f"{xlo:.0f} Гц–{xhi:.0f} Гц"
        elif band_index == 1:
            band_name = f"{xlo:.0f} Гц–{xhi:.0f} Гц"
        else:
            band_name = f"{xlo:.0f} Гц–{xhi:.0f} Гц (Fs/2)"
        self.plot.set_view((xlo, xhi), (ylo, yhi),
                           xlabel="Частота, Гц", ylabel=ylabel)
        if self.plot.ylim[0] >= self.plot.ylim[1]:
            self.plot.set_view(ylim=(-160, 0))
        floor_profile = self.win.get_noise_floor_profile(fs)
        if floor_profile is not None:
            for ch in ("L", "R"):
                floor_trace = _noise_floor_trace(floor_profile, ch, fs, n, average=1.0,
                                                 line_bins=self.mode.get_selected() == 0)
                if floor_trace is not None:
                    # Keep the saved profile's native frequency grid; Plot handles interpolation at cursor.
                    traces.append(floor_trace)
        self.plot.set_traces(traces)
        w = dsp.bh(n)
        df = fs / n
        enbw = fs * float(np.sum(w * w)) / float(np.sum(w) ** 2)
        status = be.live_status() if hasattr(be, "live_status") else {}
        gen = self.win.live.last_generator_settings
        source = "генератор OFF" if gen is None else f"генератор {gen[0]:.0f} Гц / {gen[1]:.1f} dBFS"
        xruns = status.get("xruns")
        xrun_text = "XRUN: счётчик недоступен" if xruns is None else f"XRUN: {xruns}"
        mode_name = "линейчатый спектр" if self.mode.get_selected() == 0 else "спектральная плотность шума"
        floor_state = "пол АЦП: есть" if floor_profile is not None else "пол АЦП: не калиброван"
        self.state.set_text(f"{source} · {mode_name} · {floor_state} · полоса {band_name} · N={n:,} · "
                            f"Δf={df:.3f} Гц · ENBW={enbw:.2f} Гц · "
                            f"окно {n/fs:.3f} с · обновление ≥{refresh_ms} мс · {xrun_text} · "
                            f"ADC frames={status.get('capture_frames', 0):,}")
        return True


class ThdPage(Page):
    def __init__(self, win):
        self.f = spin("Частота, Гц", 10, 40000, 1, 997, 0)
        self.lv = spin("Уровень генератора, dBFS", -100, -0.5, 0.5, -6, 1)
        self.route = combo("Канал", ["L+R", "L", "R"], 0)
        self.lvls = Adw.EntryRow(title="Уровни для THD(уровень), dBFS")
        self.lvls.set_name(_pref_widget_name("Уровни для THD(уровень), dBFS"))
        self.lvls.set_text("-6, -12, -18, -24, -36, -48")
        plot = Plot(True, "Гц", "dBFS")
        plot.set_view((10, 40000), (-180, 0))
        self.result_headline = Gtk.Label(label="Задай частоту и уровень. Будет подан синус на выбранный канал(ы); измеряются THD, THD+N, гармоники и фон.", xalign=0, wrap=True)
        self.result_headline.add_css_class("title-4")
        super().__init__(win, group("Тестовый синус", self.f, self.lv, self.route),
                         button("ИЗМЕРИТЬ THD / THD+N", self.go, True),
                         self.result_headline,
                         group("Серия THD при разных уровнях", self.lvls),
                         button("ИЗМЕРИТЬ THD ПО СПИСКУ УРОВНЕЙ", self.go_level),
                         plot=plot)

    def go(self):
        f, lv, r = self.f.get_value(), self.lv.get_value(), ("both", "L", "R")[self.route.get_selected()]
        self.win.run_job(lambda rig: m.thd_point(rig, f, lv, r), self.done)

    def done(self, res):
        self.win.last = res
        self.plot.xlog = True
        traces = [{"x": r["freq"][1:], "y": r["spec_db"][1:], "label": c, "color": c, "width": 1.0}
                  for c, r in res["ch"].items()]
        floor_profile = self.win.get_noise_floor_profile(self.win.rig_fs(), fresh=True)
        rows = []
        for channel, r in res["ch"].items():
            thd_text = "ниже порога" if r.get("thd_bound") else f"{r['thd_pct']:.4f}%"
            rows.append(f"{channel}: THD {thd_text} · THD+N {r['thdn_pct']:.4f}%")
        self.result_headline.set_text("ИЗМЕРЕНО · " + "   |   ".join(rows))
        if floor_profile is None:
            self.say(m.text_thd(res) + "\n\nПол АЦП не откалиброван для этой карты/Fs или настройки микшера изменились. "
                     "Пунктирная линия появится после измерения шума в Setup.")
        else:
            self.say(m.text_thd(res))
        if floor_profile is not None:
            n = int(res["ch"][next(iter(res["ch"]))].get("n", self.win.rig_fs()))
            for ch in ("L", "R"):
                floor_trace = _noise_floor_trace(floor_profile, ch, self.win.rig_fs(), n, average=1.0, line_bins=True)
                if floor_trace is not None:
                    traces.append(floor_trace)
        ylo, yhi = _spectrum_ylim(traces, top=0.0)
        self.plot.xlog = True
        self.plot.set_view((10, 40000), (ylo, yhi), ylabel="dBFS/bin · текущая полоса разрешения")
        self.plot.set_traces(traces)

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
        self.lv = spin("Уровень генератора @1 кГц, dBFS", -60, -0.5, 0.5, -6, 1,
                       "Обычная АЧХ начинается с −6 dBFS. В режиме inverse-RIAA высокие частоты предыскажаются и могут автоматически ослабляться по пику.")
        self.mode = combo("Тестовый сигнал", ["Плоский свип · обычная АЧХ", "Inverse-RIAA · фонокорректор"], 0,
                          "Плоский свип — общий режим по умолчанию. Inverse-RIAA выбирай вручную для проверки фонокорректора по стандартной RIAA-кривой.")
        self.iec = combo("Эталон (необязательно)", ["Не сравнивать с RIAA", "RIAA · стандарт", "RIAA + IEC · срез НЧ"], 0,
                         "По умолчанию RIAA не применяется. Выбери её только если проверяешь фонокорректор и хочешь оценить отклонение от этой коррекции.")
        self.view = combo("Показывать", self.VIEWS, 1)
        self.res = None
        self.ref = None
        plot = Plot(True, "Гц", "Отклонение от RIAA, дБ")
        plot.set_view((20, 20000), (-0.5, 0.5))
        super().__init__(win, group("Свип (по Фарине)", self.lo, self.hi, self.dur, self.lv, self.mode, self.iec),
                         button("Измерить", self.go, True), group("Вид", self.view),
                         self._buttons(), plot=plot)
        self.view.connect("notify::selected", self.on_view_changed)
        self.iec.connect("notify::selected", self.on_view_changed)

    def on_view_changed(self, *_):
        # Y ranges have different units in deviation / THD / phase views.
        # Do not carry a manually entered dB range into degrees or percent.
        self.plot.clear_manual_ylim()
        self.draw()

    def _buttons(self):
        box = Gtk.Box(spacing=8)
        box.append(button("Закрепить как эталон", self.pin))
        box.append(button("Сбросить", self.unpin))
        box.append(button("Сохранить", self.save))
        return box

    def go(self):
        mode = ("flat", "riaa")[self.mode.get_selected()]
        target = self.iec.get_selected()
        if mode == "riaa" and target == 0:
            self.say("Inverse-RIAA требует явно выбранного эталона: RIAA или RIAA + IEC.")
            return
        a = dict(f_lo=self.lo.get_value(), f_hi=self.hi.get_value(), dur=self.dur.get_value(),
                 level_1k_dbfs=self.lv.get_value(), mode=mode,
                 iec=(target == 2))
        profile, profile_note = self.win.get_response_calibration(self.win.rig_fs())
        self.win.run_job(lambda rig: m.sweep(rig, response_calibration=profile, **a),
                         lambda result: self.done_with_calibration(result, profile_note),
                         "АЧХ: измеряю свип и проверяю профиль карты…")

    def done_with_calibration(self, res, profile_note):
        if profile_note:
            res["calibration_note"] = profile_note
        self.done(res)

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
        if v == 0 and self.iec.get_selected() == 0:
            self.plot.set_traces([])
            self.say("Для отклонения выбери эталон RIAA или RIAA + IEC. Обычная АЧХ доступна без эталона.")
            return
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
        if v == 0:
            traces.append({"x": [self.lo.get_value(), self.hi.get_value()], "y": [0.0, 0.0],
                           "label": "0 дБ · норма", "color": "ref", "dash": True, "width": 1.0})
        f_lo = (self.res or self.ref)["f_lo"]
        f_hi = (self.res or self.ref)["f_hi"]
        self.plot.set_view((f_lo, f_hi), ylabel=ylabel)
        self.plot.set_traces(traces)
        if v == 0:
            # For a phono preamp, the useful question is normally a small
            # deviation around the RIAA target, not a broad ±3 dB overview.
            self.plot.set_view(ylim=(-0.5, 0.5))
        elif v == 1:
            self.plot.set_view(ylim=(-0.5, 0.5))
        elif v == 2:
            self.plot.autoscale_y(min_span=10.0)
        elif v == 3:
            self.plot.autoscale_y(min_span=0.01)
        else:
            self.plot.set_view(ylim=(-180.0, 180.0))


class ImdPage(Page):
    def __init__(self, win):
        self.preset = combo("Метод", list(m.IMD_PRESETS), 0)
        self.f1 = spin("f1, Гц", 5, 40000, 10, 19000, 0)
        self.f2 = spin("f2, Гц", 5, 40000, 10, 20000, 0)
        self.mode = combo("Соотношение", ["1:1 (CCIF)", "4:1 (SMPTE/DIN)"], 0)
        self.lv = spin("Пиковый уровень суммы двух тонов, dBFS", -100, -0.5, 0.5, -6, 1)
        plot = Plot(True, "Гц", "dBFS (линия)")
        plot.set_view((10, 48000), (-180, 0))
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
        traces = [{"x": r["freq"][1:], "y": r["spec_db"][1:], "label": c, "color": c, "width": 1.0}
                  for c, r in res["ch"].items()]
        floor_profile = self.win.get_noise_floor_profile(self.win.rig_fs(), fresh=True)
        if floor_profile is None:
            self.say(m.text_imd(res) + "\n\nПол АЦП не измерен/не совпадает с настройками карты. "
                     "Для пунктирной кривой измерь его на странице «Калибровка / Setup».")
        else:
            self.say(m.text_imd(res) + "\n\nПунктир — собственный шум АЦП этой карты, пересчитанный в полосу разрешения усреднённого спектра IMD.")
        if floor_profile is not None:
            n = int(getattr(self.win.rig, "n", self.win.rig_fs()))
            blocks = max(1, int(getattr(self.win.rig, "blocks", 1)))
            for ch in ("L", "R"):
                floor_trace = _noise_floor_trace(floor_profile, ch, self.win.rig_fs(), n,
                                                 average=blocks, line_bins=True)
                if floor_trace is not None:
                    floor_trace["label"] += f" · ENBW/{blocks}"
                    traces.append(floor_trace)
        ylo, yhi = _spectrum_ylim(traces, top=0.0)
        self.plot.xlog = True
        self.plot.set_view((10, min(0.45 * self.win.rig_fs(), 48000)), (ylo, yhi),
                           ylabel="dBFS/bin · текущая полоса разрешения")
        self.plot.set_traces(traces)


class NoisePage(Page):
    def __init__(self, win):
        self.sec = spin("Длительность, с", 2, 60, 1, 8, 0, "вход корректора закорочен или нагружен источником")
        plot = Plot(True, "Гц", "dBFS/√Гц")
        plot.set_view((50, 20000), (-150, 0))
        self.xtalk_summary = Gtk.Label(label="Проникновение каналов ещё не измерено. Измеряются L→R и R→L на 100 Гц, 1 кГц и 10 кГц.",
                                       xalign=0, wrap=True)
        self.xtalk_summary.add_css_class("title-4")
        super().__init__(win, group("Собственный шум входов", self.sec),
                         button("ИЗМЕРИТЬ ШУМ АЦП", self.go, True),
                         group("Проникновение между каналами", Adw.ActionRow(title="Тест: 100 Гц · 1 кГц · 10 кГц")),
                         button("ИЗМЕРИТЬ ПРОНИКНОВЕНИЕ L↔R", self.go_x),
                         self.xtalk_summary, plot=plot)

    def go(self):
        s = self.sec.get_value()
        self.win.run_job(lambda rig: m.noise(rig, s), self.done)

    def done(self, res):
        self.win.last = res
        self.plot.clear_manual_ylim()
        self.plot.xlog = True
        self.plot.set_view((20, min(20000, 0.45 * self.win.rig_fs())), (-180, -60),
                           xlabel="Частота, Гц", ylabel="Плотность шума, dBFS/√Гц")
        self.say(m.text_noise(res))
        traces = [{"x": r["freq"][1:], "y": r["dens_dbfs"][1:], "label": c, "color": c, "width": 1.2}
                  for c, r in res["ch"].items()]
        ylo, yhi = _spectrum_ylim(traces, top=-60.0)
        self.plot.set_view(ylim=(ylo, yhi))
        self.plot.set_traces(traces)

    def go_x(self):
        def done(result):
            self.win.last = result
            self.say(m.text_crosstalk(result))
            self.xtalk_summary.set_text("РЕЗУЛЬТАТ ПРОНИКНОВЕНИЯ · " + "   |   ".join(
                f"{row['f']:.0f} Гц: L→R {row['L→R']:.1f} дБ / R→L {row['R→L']:.1f} дБ"
                for row in result["rows"]))
            freqs = [row["f"] for row in result["rows"]]
            self.plot.xlog = True
            self.plot.clear_manual_ylim()
            values = [float(row[key]) for row in result["rows"] for key in ("L→R", "R→L")]
            y_min, y_max = min(values), max(values)
            span = max(6.0, y_max - y_min)
            self.plot.set_view((min(freqs) * 0.8, max(freqs) * 1.25),
                               (y_min - 0.18 * span, y_max + 0.18 * span),
                               xlabel="Частота, Гц", ylabel="Проникновение относительно основного канала, дБ")
            self.plot.set_traces([
                {"x": freqs, "y": [row["L→R"] for row in result["rows"]],
                 "label": "L → R", "color": "L", "width": 2.6, "marker": True},
                {"x": freqs, "y": [row["R→L"] for row in result["rows"]],
                 "label": "R → L", "color": "R", "width": 2.6, "marker": True},
            ])
        self.win.run_job(lambda rig: m.crosstalk(rig), done, "измеряю проникновение между каналами")


class NoiseResponsePage(Page):
    """Одноразовая и живая АЧХ по известному шуму: H1 transfer + coherence."""

    VIEWS = ["Отклонение от RIAA, дБ", "АЧХ, нормированная на 1 кГц", "Когерентность"]

    def __init__(self, win):
        self.sec = spin("Длительность измерения, с", 4, 60, 1, 8, 0,
                        "Одноразовый прогон; Live использует окно 4–8 с")
        self.kind = combo("Тестовый сигнал", ["Розовый шум", "Белый шум", "Шум inverse-RIAA"], 0)
        self.level = spin("Желаемый RMS-уровень шума, dBFS", -60, -3, 1, -12, 0,
                          "Это RMS, а не пик синуса. Если случайный шум превышает −3 dBFS peak, программа уменьшит его и отдельно покажет фактический RMS.")
        self.lo = spin("Нижняя частота, Гц", 10, 1000, 1, 20, 0)
        self.hi = spin("Верхняя частота, Гц", 1000, 40000, 100, 20000, 0)
        self.iec = combo("Сравнение с коррекцией", ["Без эталона", "RIAA", "RIAA + IEC"], 0,
                         "По умолчанию показывается обычная АЧХ L/R. RIAA/IEC — дополнительные режимы, выбираются вручную.")
        self.view = combo("График", self.VIEWS, 1)
        self.plot = Plot(True, "Частота, Гц", "Отклонение от RIAA, дБ")
        self.plot.set_view((20, 20000), (-0.5, 0.5))
        self.live_toggle = Gtk.ToggleButton.new_with_label("Живая АЧХ")
        self.live_toggle.add_css_class("suggested-action")
        self.live_hint = Gtk.Label(label="Live подаёт непрерывный шум через ЦАП и обновляет H1 по входу АЦП.",
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
        self.view.connect("notify::selected", self.on_view_changed)
        self.live_toggle.connect("toggled", self.on_live_toggle)
        self.res = None
        self.live_period = None
        self.live_info = None
        self.live_timer = None
        self.live_busy = False
        self.live_sequence = 0

    def on_view_changed(self, *_):
        # Units change between dB and coherence; restore that view's useful default.
        self.plot.clear_manual_ylim()
        self.draw()

    def _settings(self):
        if self.hi.get_value() <= self.lo.get_value():
            raise ValueError("Верхняя частота должна быть выше нижней")
        fs = self.win.rig_fs()
        if self.hi.get_value() >= 0.45 * fs:
            raise ValueError(f"Верхняя частота должна быть ниже 0.45·Fs ({0.45 * fs:.0f} Гц)")
        kind = ("pink", "white", "riaa")[self.kind.get_selected()]
        reference = self.iec.get_selected()
        iec = reference == 2
        if kind == "riaa" and reference == 0:
            raise ValueError("Для шума inverse-RIAA выбери эталон RIAA или RIAA + IEC. Для обычной АЧХ оставь розовый/белый шум.")
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
        profile, profile_note = self.win.get_response_calibration(self.win.rig_fs())
        self.win.run_job(lambda rig: m.noise_response(rig, *args, response_calibration=profile),
                         lambda result: self.done_with_calibration(result, profile_note), "Шумовая АЧХ…")

    def done_with_calibration(self, res, profile_note):
        if profile_note:
            res["calibration_note"] = profile_note
        res["has_riaa_reference"] = self.iec.get_selected() in (1, 2)
        self.done(res)

    def done(self, res):
        self.res = res
        res["display_view"] = self.view.get_selected()
        self.win.last = res
        self.say(m.text_noise_response(res))
        self.draw()

    def draw(self):
        if self.res is None:
            return
        traces = []
        key = ("riaa_dev_db", "fr_norm_db", "coherence")[self.view.get_selected()]
        if key == "riaa_dev_db" and not self.res.get("has_riaa_reference", False):
            self.plot.set_traces([])
            self.say("Отклонение от RIAA не выбрано: оставь обычную нормированную АЧХ или выбери RIAA/IEC в поле «Сравнение с коррекцией» и повтори измерение.")
            return
        default_ylim = ((0, 1.05) if key == "coherence" else
                        (-0.5, 0.5) if key == "fr_norm_db" else (-0.5, 0.5))
        self.plot.set_view((self.lo.get_value(), self.hi.get_value()), default_ylim,
                           ylabel="Когерентность" if key == "coherence" else "дБ")
        for ch in ("L", "R"):
            r = self.res["ch"][ch]
            traces.append({"x": r["freq"], "y": r[key], "label": ch, "color": ch, "width": 1.5})
        if key == "riaa_dev_db":
            traces.append({"x": [self.lo.get_value(), self.hi.get_value()], "y": [0.0, 0.0],
                           "label": "0 дБ · норма", "color": "ref", "dash": True, "width": 1.0})
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
        iec = self.iec.get_selected() == 2
        sequence = self.live_sequence
        response_profile, response_note = self.win.get_response_calibration(fs)

        def work():
            try:
                result = m.noise_transfer_capture(source, rec, fs, lo, hi, iec)
                if response_profile is not None:
                    m.apply_response_calibration(result, response_profile)
                elif response_note:
                    result.setdefault("warnings", []).append(response_note)
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
        self._vinyl_started = time.monotonic()
        self.set_job_busy(True)
        self.set_job_status("running", "читаю WAV и выполняю анализ")
        self.win.status.set_text("Анализ TACET…")
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
        elapsed = time.monotonic() - getattr(self, "_vinyl_started", time.monotonic())
        if err is not None:
            self.win.status.set_text("Ошибка анализа")
            self.say(f"Ошибка анализа WAV:\n{type(err).__name__}: {err}")
            self.set_job_status("error", f"{type(err).__name__}: {err}")
            self.set_job_busy(False)
            return False
        self.win.status.set_text(f"Готово · {elapsed:.1f} с")
        self.say(vinyl.text_result(result))
        self.set_job_status("success", "", elapsed)
        self.set_job_busy(False)
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
            self.plot.set_view((5, 15), (-160, 0), ylabel="dBFS/sqrt(Гц)")
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
    """Per-device voltage, frequency-response and ADC noise calibration."""

    def __init__(self, win):
        self.win = win
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
        self.auto_response = Adw.SwitchRow(title="Автоматически применять калибровку АЧХ/фазы этой карты",
                                          subtitle="Отдельный профиль для каждой физической карты и Fs. Выключи, чтобы видеть тракт без поправки.")
        self.auto_response.set_active(bool(c.get("auto_apply_response", True)))
        self.auto_noise = Adw.SwitchRow(title="Автоматически показывать шумовой пол этой карты",
                                       subtitle="Профиль привязан к физической карте, Fs и состоянию микшера.")
        self.auto_noise.set_active(bool(c.get("auto_show_noise_floor", True)))
        self.card_status = Gtk.Label(label="", xalign=0, wrap=True)
        self.card_status.add_css_class("title-4")
        self.fr_status = Adw.ActionRow(title="Калибровка АЧХ карты")
        self.noise_status = Adw.ActionRow(title="Шумовой пол АЦП")
        self.fr_status.set_subtitle("Для текущей карты и частоты калибровка ещё не выполнена")
        self.noise_status.set_subtitle("Для текущей карты и частоты измерение ещё не выполнено")
        info = Gtk.Label(
            label=("АЧХ/ФАЗА ИЗМЕРИТЕЛЬНОЙ КАРТЫ. Соедини ЦАП L→АЦП L и ЦАП R→АЦП R напрямую. "
                   "Нажми кнопку калибровки один раз для каждой физической карты и каждой нужной частоты дискретизации. "
                   "Профили сохраняются в ~/.config/valvebench/config.json отдельно по устойчивому ID карты и Fs; "
                   "при выборе этой карты калибровка подхватывается автоматически, даже если номер hw изменился. "
                   "Коррекция выравнивает частотный отклик и фазу ЦАП+АЦП по каждому каналу, но не калибрует абсолютные вольты. "
                   "Если изменятся уровни/маршрутизация микшера, профиль будет отклонён до повторной калибровки.\n\n"
                   "ШУМОВОЙ ПОЛ АЦП. Перед измерением отключи DUT и замкни входы АЦП либо подключи эквивалентное сопротивление источника. "
                   "Профиль также сохраняется отдельно для карты и Fs. Он показывается пунктиром в Спектре, THD и IMD; "
                   "это один и тот же измеренный спектральный шум, пересчитанный под разрешение конкретного графика. "
                   "Он не вычитается из результата и не заменяет шум проверяемого устройства."),
            xalign=0, wrap=True)
        info.add_css_class("dim-label")
        super().__init__(win,
                         group("Измерительные параметры", self.mains, self.band, self.att),
                         group("Калибровка ЦАП (для абсолютного напряжения)", self.out_l, self.out_r),
                         group("Калибровка АЦП (для абсолютного напряжения)", self.in_l, self.in_r),
                         group("Показания мультиметра при −6 dBFS", self.meter_l, self.meter_r),
                         button("Применить показания мультиметра к DAC L/R", self.cal_out),
                         button("Калибровать ADC L/R по петле DAC→ADC", self.cal_in),
                         group("Автоматическая коррекция и отображение", self.auto_response, self.auto_noise),
                         self.card_status, info, self.fr_status,
                         button("1. КАЛИБРОВАТЬ АЧХ ЗВУКОВОЙ КАРТЫ · ПРЯМАЯ ПЕТЛЯ", self.calibrate_response, True),
                         self.noise_status,
                         button("2. ИЗМЕРИТЬ ШУМОВОЙ ПОЛ АЦП", self.calibrate_noise),
                         Gtk.Label(label=("Проверка тактов сравнивает фактическую частоту тестового тона в записи АЦП "
                                          "с частотой, которую выдаёт ЦАП. Это проверка расхождения частот дискретизации, "
                                          "а не синхронизации часов компьютера."), xalign=0, wrap=True),
                         button("ПРОВЕРИТЬ СОВПАДЕНИЕ ЧАСТОТ ЦАП И АЦП", self.check_clock),
                         button("Сохранить настройки", self.apply))
        self.auto_response.connect("notify::active", self.on_auto_changed)
        self.auto_noise.connect("notify::active", self.on_auto_changed)
        self.refresh_from_calibration()

    def on_auto_changed(self, *_):
        self.win.cal["auto_apply_response"] = self.auto_response.get_active()
        self.win.cal["auto_show_noise_floor"] = self.auto_noise.get_active()
        calib.save(self.win.cal)

    def refresh_from_calibration(self):
        c = self.win.cal
        self.mains.set_selected(0 if c["mains"] == 50 else 1)
        self.band.set_value(float(c["band_hi"]))
        self.att.set_value(float(c["att_db"]))
        self.out_l.set_value(float(c["out_fs_vrms"][0] or 0))
        self.out_r.set_value(float(c["out_fs_vrms"][1] or 0))
        self.in_l.set_value(float(c["in_fs_vrms"][0] or 0))
        self.in_r.set_value(float(c["in_fs_vrms"][1] or 0))
        self.auto_response.set_active(bool(c.get("auto_apply_response", True)))
        self.auto_noise.set_active(bool(c.get("auto_show_noise_floor", True)))
        fs = self.win.rig_fs()
        hw, label = self.win.cards[self.win.dev.get_selected()]
        self.card_status.set_text(f"ТЕКУЩАЯ КАРТА: {label}\nПрофиль: {stable_device_identity(hw)} · Fs {fs/1000:g} кГц")
        fr = c.get("frequency_response_profiles", {}).get(str(fs))
        nf = c.get("noise_floor_profiles", {}).get(str(fs))
        current_fp = None
        if hw != "sim" and (fr or nf):
            current_fp = gainmatch.mixer_fingerprint_from_hw(hw)

        def profile_state(profile, auto_enabled):
            if not profile:
                return "калибровка не выполнена"
            if profile.get("device_key") != stable_device_identity(hw) or int(profile.get("fs", -1)) != int(fs):
                return "профиль принадлежит другой карте или частоте"
            expected = profile.get("mixer_fingerprint")
            if hw != "sim" and (not expected or current_fp is None or current_fp != expected):
                return "настройки микшера изменились — нужна повторная калибровка"
            if not auto_enabled:
                return "профиль сохранён, автоматическое применение выключено"
            return "профиль активен · будет применён автоматически"

        if fr:
            self.fr_status.set_subtitle(
                f"{fr.get('f_lo', 0):.0f}–{fr.get('f_hi', 0):.0f} Гц · {fr.get('created', 'дата не указана')} · "
                + profile_state(fr, self.auto_response.get_active()))
        else:
            self.fr_status.set_subtitle(f"Для {fs/1000:g} кГц ещё нет профиля")
        if nf:
            self.noise_status.set_subtitle(
                f"{nf.get('created', 'дата не указана')} · "
                + profile_state(nf, self.auto_noise.get_active()))
        else:
            self.noise_status.set_subtitle(f"Для {fs/1000:g} кГц ещё нет измерения")

    def apply(self):
        c = self.win.cal
        c["mains"] = 50.0 if self.mains.get_selected() == 0 else 60.0
        c["band_hi"] = self.band.get_value()
        c["att_db"] = self.att.get_value()
        c["out_fs_vrms"] = [self.out_l.get_value() or None, self.out_r.get_value() or None]
        c["in_fs_vrms"] = [self.in_l.get_value() or None, self.in_r.get_value() or None]
        c["auto_apply_response"] = self.auto_response.get_active()
        c["auto_show_noise_floor"] = self.auto_noise.get_active()
        calib.save(c)
        self.win.toast("Настройки и калибровки сохранены")

    def calibrate_response(self):
        dev = self.win.cards[self.win.dev.get_selected()][0]
        key = stable_device_identity(dev)
        fs = self.win.rig_fs()
        hw = dev
        before_fp = gainmatch.mixer_fingerprint_from_hw(hw)
        self.fr_status.set_subtitle("Выполняется плоский свип по прямой петле…")

        def job(rig):
            if getattr(rig.be, "hw", "sim") != hw:
                raise m.MeasureError("выбранная звуковая карта изменилась; выбери её снова")
            res = m.sweep(rig, f_lo=20.0, f_hi=min(20000.0, 0.40 * rig.fs), dur=12.0,
                          level_1k_dbfs=-12.0, mode="flat", iec=False)
            after_fp = gainmatch.mixer_fingerprint_from_hw(hw)
            if hw != "sim" and (before_fp is None or after_fp is None):
                raise m.MeasureError("не удалось прочитать состояние ALSA-микшера; профиль не сохранён")
            if before_fp and after_fp != before_fp:
                raise m.MeasureError("настройки микшера изменились во время калибровки; профиль не сохранён")
            profile = m.build_response_calibration(res, key, after_fp, fs=rig.fs)
            profile["mixer_fingerprint"] = after_fp
            return profile

        def done(profile):
            self.win.cal["frequency_response_profiles"][str(fs)] = profile
            self.win._mixer_fingerprint_cache.clear()
            calib.save(self.win.cal)
            self.refresh_from_calibration()
            self.say("КАЛИБРОВКА АЧХ/ФАЗЫ СОХРАНЕНА ДЛЯ ЭТОЙ КАРТЫ И ЧАСТОТЫ. "
                     "Теперь можно подключить проверяемое устройство. При включённой автокоррекции "
                     "измерение будет очищено от собственного частотного отклика ЦАП/АЦП отдельно для L и R.")
            self.win.toast("Профиль АЧХ карты сохранён")

        self.win.run_job(job, done, "калибрую АЧХ/фазу карты по плоской петле L→L, R→R")

    def calibrate_noise(self):
        dev = self.win.cards[self.win.dev.get_selected()][0]
        key = stable_device_identity(dev)
        fs = self.win.rig_fs()
        before_fp = gainmatch.mixer_fingerprint_from_hw(dev)
        self.noise_status.set_subtitle("Записываю 12 секунд тишины с входами, нагруженными на землю/сопротивление…")

        def job(rig):
            res = m.noise(rig, seconds=12.0)
            after_fp = gainmatch.mixer_fingerprint_from_hw(dev)
            if dev != "sim" and (before_fp is None or after_fp is None):
                raise m.MeasureError("не удалось прочитать состояние ALSA-микшера; шумовой профиль не сохранён")
            if before_fp and after_fp != before_fp:
                raise m.MeasureError("микшер изменился во время записи; шумовой профиль не сохранён")
            if any("ПЕРЕГРУЗКА" in str(w).upper() for w in res.get("warnings", [])):
                raise m.MeasureError("АЦП перегружен во время измерения шума; профиль не сохранён")
            profile = {
                "kind": "adc_noise_floor", "schema": 1, "device_key": key,
                "fs": int(rig.fs), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mixer_fingerprint": after_fp,
                "frequency_hz": np.asarray(res["ch"]["L"]["freq"], dtype=float).tolist(),
                "channels": {ch: {"density_dbfs_sqrt_hz": np.asarray(res["ch"][ch]["dens_dbfs"], dtype=float).tolist()}
                             for ch in ("L", "R")},
            }
            return profile, res

        def done(result):
            profile, measured = result
            self.win.cal["noise_floor_profiles"][str(fs)] = profile
            self.win._mixer_fingerprint_cache.clear()
            calib.save(self.win.cal)
            self.refresh_from_calibration()
            self.say("ШУМОВОЙ ПОЛ АЦП СОХРАНЁН ДЛЯ ЭТОЙ КАРТЫ И ЧАСТОТЫ. Он будет показан пунктиром "
                     "в Спектре, THD и IMD. Это одна и та же плотность шума; высота линии в dBFS/bin "
                     "зависит от полосы разрешения FFT.\n\n" + m.text_noise(measured))
            self.win.toast("Шумовой профиль карты сохранён")

        self.win.run_job(job, done, "измеряю шумовой пол АЦП (входы должны быть нагружены)")

    def check_clock(self):
        self.win.run_job(m.clock_check, lambda result: self.say(m.text_clock(result)),
                         "проверяю согласование тактов ЦАП и АЦП")

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
        self.win.run_job(job, done, "калибровка ADC по петле…")


# --------------------------------------------------------------------------- окно


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ValveBench", default_width=1320, default_height=760)
        self.cal = calib.load()
        self.ui_prefs = preferences.load()
        migrated_prefs = preferences.migrate_0_1_16_defaults(self.ui_prefs)
        if migrated_prefs != self.ui_prefs:
            self.ui_prefs = migrated_prefs
            preferences.save(self.ui_prefs)
        self._mixer_fingerprint_cache = {}
        self.be = None
        self.rig = None
        self.busy = False
        self.live_on = False
        self.last = None
        self.cards = [("sim", "Симулятор (демо без карты)")] + list_duplex_devices()
        self.toasts = Adw.ToastOverlay()

        header = Adw.HeaderBar()
        self.dev = Gtk.DropDown.new_from_strings([n for _, n in self.cards])
        saved_device = self.cal["device"]
        saved_key = self.cal.get("device_key", "")
        card_keys = [stable_device_identity(hw) for hw, _label in self.cards]
        sel = next((i for i, key in enumerate(card_keys) if saved_key and key == saved_key), -1)
        if sel < 0:
            sel = next((i for i, (d, _) in enumerate(self.cards) if d == saved_device), 0)
        selected_hw = self.cards[sel][0]
        if selected_hw == "sim" and saved_device != "sim":
            self.invalid_saved_device = saved_device
        else:
            self.invalid_saved_device = None
        self.dev.set_selected(sel)
        self.cal.activate_profile(stable_device_identity(selected_hw), selected_hw)
        self.rate_values = self._rates_for_device(selected_hw)
        self.rate = Gtk.DropDown.new_from_strings([f"{r / 1000:g} кГц" for r in self.rate_values])
        preferred_rate = int(self.cal["fs"])
        rate_sel = (self.rate_values.index(preferred_rate) if preferred_rate in self.rate_values
                    else min(range(len(self.rate_values)), key=lambda i: abs(self.rate_values[i] - preferred_rate)))
        self.rate.set_selected(rate_sel)
        self.cal["fs"] = self.rate_values[rate_sel]
        self._updating_rate = False
        self.live_btn = Gtk.ToggleButton(label="АУДИОПОТОК: ОТКРЫТЬ")
        self.live_btn.set_tooltip_text("Открыть непрерывный поток звука: АЦП начинает захват входа, ЦАП остаётся в тишине до включения генератора. Сигнал измерения подаётся отдельной кнопкой генератора.")
        self.live_btn.add_css_class("pill")
        self.live_btn.connect("toggled", self.on_live)
        self.generator_btn = Gtk.ToggleButton(label="▶  ПОДАТЬ ТЕСТОВЫЙ СИГНАЛ")
        self.generator_btn.add_css_class("pill")
        self.generator_btn.set_sensitive(False)
        self.generator_btn.set_tooltip_text(
            "Включить тестовый сигнал на выходах ЦАП. Сначала включи аудиопоток и проверь соединение/уровень.")
        self.generator_btn.connect("toggled", self.on_generator_toggle)
        self.status = Gtk.Label(label="", xalign=1)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.dev)
        header.pack_start(self.rate)
        header.pack_start(self.live_btn)
        header.pack_end(self.spinner)
        header.pack_end(self.status)
        self.dev.connect("notify::selected", self.on_device_changed)
        self.rate.connect("notify::selected", self.on_rate_changed)

        self.stack = Gtk.Stack()
        self.live = LivePage(self)
        self.spectrum = SpectrumPage(self)
        self.noise_response_page = NoiseResponsePage(self)
        self.setup = SetupPage(self)
        pages = (
            ("live", "Gain Match / Уровень", self.live),
            ("spectrum", "Спектр (большой экран)", self.spectrum),
            ("thd", "THD / THD+N", ThdPage(self)),
            ("resp", "АЧХ / RIAA / Sweep", ResponsePage(self)),
            ("noise_response", "АЧХ шумом / H1", self.noise_response_page),
            ("imd", "IMD", ImdPage(self)),
            ("noise", "Шум / Crosstalk", NoisePage(self)),
            ("vinyl", "TACET / Vinyl Test", VinylPage(self)),
            ("setup", "Калибровка / Setup", self.setup),
        )
        self.page_map = {name: page for name, _title, page in pages}
        for name, title, page in pages:
            self.stack.add_titled(page, name, title)
        self._restore_and_bind_preferences()
        side = Gtk.StackSidebar(stack=self.stack)
        side.set_size_request(200, -1)
        body = Gtk.Box()
        body.set_hexpand(True)
        body.set_vexpand(True)
        body.append(side)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.stack.connect("notify::visible-child", self.on_page_changed)
        body.append(self.stack)
        self.toasts.set_child(body)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(self.toasts)
        self.set_content(tv)
        self.connect("close-request", self.on_close)
        if self.invalid_saved_device:
            self.toast(f"Сохранённое устройство {self.invalid_saved_device} не найдено. Выбран симулятор; выбери нужную карту вверху.")

    @staticmethod
    def _widget_tree(root):
        child = root.get_first_child()
        while child is not None:
            yield child
            yield from Window._widget_tree(child)
            child = child.get_next_sibling()

    def _device_scope(self):
        """Preference scope for card-dependent display ranges, not ALSA card numbers."""
        dev = self.cards[self.dev.get_selected()][0]
        return f"{stable_device_identity(dev)}@{int(self.rig_fs())}"

    def _device_pref_key(self, page_key, name):
        return f"{page_key}/device/{self._device_scope()}/{name}"

    def _restore_and_bind_preferences(self):
        """Restore named settings; Y ranges and FFT floor/top are per card and Fs."""
        for page_key, page in self.page_map.items():
            if page_key == "setup":  # physical calibration fields live in the per-card profile
                continue
            for widget in self._widget_tree(page):
                name = widget.get_name() if hasattr(widget, "get_name") else ""
                if not name or not (name.startswith("pref_") or name in ("plot_y_min", "plot_y_max")):
                    continue
                card_scoped = (page_key == "spectrum" and name in
                               ("pref_spectrum_floor", "pref_spectrum_top"))
                key = self._device_pref_key(page_key, name) if card_scoped else f"{page_key}/{name}"
                legacy_key = f"{page_key}/{name}"
                if key in self.ui_prefs:
                    value = self.ui_prefs[key]
                elif card_scoped and legacy_key in self.ui_prefs:
                    value = self.ui_prefs[legacy_key]
                    self.ui_prefs[key] = value
                else:
                    value = None
                if value is not None:
                    try:
                        if isinstance(widget, Adw.SpinRow):
                            widget.set_value(float(value))
                        elif isinstance(widget, Adw.ComboRow):
                            count = widget.get_model().get_n_items()
                            if 0 <= int(value) < count:
                                widget.set_selected(int(value))
                        elif isinstance(widget, Adw.EntryRow):
                            widget.set_text(str(value))
                        elif isinstance(widget, Gtk.DropDown):
                            count = widget.get_model().get_n_items()
                            if 0 <= int(value) < count:
                                widget.set_selected(int(value))
                        elif isinstance(widget, Gtk.SpinButton):
                            widget.set_value(float(value))
                    except (TypeError, ValueError, AttributeError):
                        pass
                if card_scoped and isinstance(widget, Gtk.DropDown):
                    widget.connect("notify::selected",
                                   lambda w, _p, pk=page_key, nm=name:
                                   self._store_pref(self._device_pref_key(pk, nm), w.get_selected()))
                elif isinstance(widget, Adw.SpinRow):
                    widget.connect("notify::value", lambda w, _p, k=key: self._store_pref(k, w.get_value()))
                elif isinstance(widget, (Adw.ComboRow, Gtk.DropDown)):
                    widget.connect("notify::selected", lambda w, _p, k=key: self._store_pref(k, w.get_selected()))
                elif isinstance(widget, Adw.EntryRow):
                    widget.connect("notify::text", lambda w, _p, k=key: self._store_pref(k, w.get_text()))
                elif isinstance(widget, Gtk.SpinButton):
                    widget.connect("value-changed", lambda w, k=key: self._store_pref(k, w.get_value()))
            # Migrate an old global plot range to the currently selected card once.
            if isinstance(page, Page) and page.plot is not None:
                scale_key = self._device_pref_key(page_key, "plot_scale")
                scale = self.ui_prefs.get(scale_key)
                if scale is None:
                    scale = self.ui_prefs.get(f"{page_key}/plot_scale")
                    if isinstance(scale, dict):
                        self.ui_prefs[scale_key] = scale
                if isinstance(scale, dict):
                    view = page.view.get_selected() if hasattr(page, "view") else None
                    if scale.get("view") == view:
                        try:
                            page.plot.set_manual_ylim(float(scale["min"]), float(scale["max"]))
                        except (KeyError, TypeError, ValueError):
                            pass
        preferences.save(self.ui_prefs)

    def restore_device_scoped_preferences(self):
        """Load per-card graph ranges and FFT dB bounds after card/Fs changes."""
        scope = self._device_scope()
        for page_key, page in self.page_map.items():
            if page_key == "spectrum":
                for control_name, widget in (("pref_spectrum_floor", self.spectrum.floor),
                                             ("pref_spectrum_top", self.spectrum.top)):
                    key = f"spectrum/device/{scope}/{control_name}"
                    value = self.ui_prefs.get(key)
                    if value is not None:
                        try:
                            if 0 <= int(value) < widget.get_model().get_n_items():
                                widget.set_selected(int(value))
                        except (TypeError, ValueError):
                            pass
            if isinstance(page, Page) and page.plot is not None:
                page.plot.manual_ylim = None
                scale = self.ui_prefs.get(f"{page_key}/device/{scope}/plot_scale")
                view = page.view.get_selected() if hasattr(page, "view") else None
                if isinstance(scale, dict) and scale.get("view") == view:
                    try:
                        page.plot.set_manual_ylim(float(scale["min"]), float(scale["max"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                else:
                    page._sync_plot_scale(page.plot.ylim)

    def _store_pref(self, key, value):
        self.ui_prefs[key] = value
        try:
            preferences.save(self.ui_prefs)
        except OSError as exc:
            self.toast(f"Не удалось сохранить настройку интерфейса: {exc}")

    def persist_plot_scale(self, page):
        page_key = next((name for name, obj in self.page_map.items() if obj is page), None)
        if page_key is None or page.plot is None:
            return
        view = page.view.get_selected() if hasattr(page, "view") else None
        self.ui_prefs[self._device_pref_key(page_key, "plot_scale")] = {
            "view": view, "min": float(page.plot.ylim[0]), "max": float(page.plot.ylim[1])}
        try:
            preferences.save(self.ui_prefs)
        except OSError as exc:
            self.toast(f"Не удалось сохранить масштаб: {exc}")

    def clear_plot_scale_preference(self, page):
        page_key = next((name for name, obj in self.page_map.items() if obj is page), None)
        if page_key:
            self.ui_prefs.pop(self._device_pref_key(page_key, "plot_scale"), None)
            preferences.save(self.ui_prefs)

    def _cached_mixer_fingerprint(self, dev):
        # Live FFT asks for a noise-floor overlay several times per second.
        # Do not spawn `amixer` on every frame; re-check at most every 3 seconds.
        now = time.monotonic()
        key = str(dev)
        cached = self._mixer_fingerprint_cache.get(key)
        if cached and now - cached[0] < 3.0:
            return cached[1]
        fingerprint = gainmatch.mixer_fingerprint_from_hw(dev)
        self._mixer_fingerprint_cache[key] = (now, fingerprint)
        return fingerprint

    def get_response_calibration(self, fs):
        """Return a valid per-card/per-rate response profile or a human-readable reason."""
        if not bool(self.cal.get("auto_apply_response", True)):
            return None, "АЧХ показана без коррекции измерительной карты (автоприменение отключено)."
        dev = self.cards[self.dev.get_selected()][0]
        key = stable_device_identity(dev)
        profile = self.cal.get("frequency_response_profiles", {}).get(str(int(fs)))
        if not isinstance(profile, dict):
            return None, f"Для этой карты и {int(fs)} Гц нет калибровки АЧХ; показан сырой тракт."
        if profile.get("device_key") != key or int(profile.get("fs", -1)) != int(fs):
            return None, "Профиль АЧХ принадлежит другой карте/частоте и не применён."
        expected = profile.get("mixer_fingerprint")
        if dev != "sim" and not expected:
            return None, "У профиля нет снимка ALSA-микшера; он не применён. Повтори калибровку."
        if expected:
            # A one-shot response measurement must never use a 3-second stale
            # mixer snapshot: ALSA volume/source may have changed just before go().
            actual = gainmatch.mixer_fingerprint_from_hw(dev)
            self._mixer_fingerprint_cache[str(dev)] = (time.monotonic(), actual)
            if actual is None or actual != expected:
                return None, "Настройки микшера изменились после калибровки; АЧХ показана без старой поправки. Повтори калибровку петли."
        return profile, f"АЧХ скорректирована по калибровке {profile.get('created', '')} этой карты."

    def get_noise_floor_profile(self, fs, fresh=False):
        """Return measured card noise density only when physical card/mixer/Fs match."""
        if not bool(self.cal.get("auto_show_noise_floor", True)):
            return None
        dev = self.cards[self.dev.get_selected()][0]
        key = stable_device_identity(dev)
        profile = self.cal.get("noise_floor_profiles", {}).get(str(int(fs)))
        if not isinstance(profile, dict) or profile.get("kind") != "adc_noise_floor":
            return None
        if profile.get("device_key") != key or int(profile.get("fs", -1)) != int(fs):
            return None
        expected = profile.get("mixer_fingerprint")
        if dev != "sim" and not expected:
            return None
        if expected:
            actual = (gainmatch.mixer_fingerprint_from_hw(dev) if fresh
                      else self._cached_mixer_fingerprint(dev))
            self._mixer_fingerprint_cache[str(dev)] = (time.monotonic(), actual)
            if actual is None or actual != expected:
                return None
        return profile

    def _rates_for_device(self, dev):
        if dev == "sim":
            return list(RATES)
        advertised = supported_duplex_rates(dev)
        available = [r for r in RATES if r in advertised]
        # Some USB drivers do not expose stream0 reliably. Keep the explicit
        # selector usable; the selected PCM is probed by ALSA when opened.
        return available or list(RATES)

    def on_device_changed(self, *_):
        self._mixer_fingerprint_cache.clear()
        dev, _label = self.cards[self.dev.get_selected()]
        self.cal.activate_profile(stable_device_identity(dev), dev)
        old_rate = int(self.cal.get("fs", 96000))
        self.rate_values = self._rates_for_device(dev)
        new_index = (self.rate_values.index(old_rate) if old_rate in self.rate_values
                     else min(range(len(self.rate_values)), key=lambda i: abs(self.rate_values[i] - old_rate)))
        self._updating_rate = True
        self.rate.set_model(Gtk.StringList.new([f"{r / 1000:g} кГц" for r in self.rate_values]))
        self.rate.set_selected(new_index)
        self._updating_rate = False
        self.cal["fs"] = self.rate_values[new_index]
        self.reset_rig()
        calib.save(self.cal)
        if hasattr(self, "live"):
            self.live.refresh_gain_reference()
        if hasattr(self, "setup"):
            self.setup.refresh_from_calibration()
        if hasattr(self, "page_map"):
            self.restore_device_scoped_preferences()

    def on_rate_changed(self, *_):
        if getattr(self, "_updating_rate", False):
            return
        self.reset_rig()
        self._mixer_fingerprint_cache.clear()
        self.cal["fs"] = self.rig_fs()
        calib.save(self.cal)
        if hasattr(self, "live"):
            self.live.refresh_gain_reference()
        if hasattr(self, "setup"):
            self.setup.refresh_from_calibration()
        if hasattr(self, "page_map"):
            self.restore_device_scoped_preferences()

    def on_page_changed(self, *_):
        page = self.stack.get_visible_child()
        if page is not self.noise_response_page and self.noise_response_page.live_toggle.get_active():
            self.noise_response_page.live_toggle.set_active(False)
        self.spectrum.set_visible(page is self.spectrum)

    # ---- стенд
    def rig_fs(self):
        return self.rate_values[self.rate.get_selected()]

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

    def on_generator_toggle(self, btn):
        active = btn.get_active()
        if active and not self.live_on:
            btn.set_active(False)
            self.toast("Сначала нажми «АУДИОПОТОК: ОТКРЫТЬ». Это запускает захват АЦП и подготавливает ЦАП; тестовый сигнал пока не подаётся.")
            return
        btn.set_label("ГЕНЕРАТОР: ВКЛ" if active else "ГЕНЕРАТОР: ВЫКЛ")
        if active:
            btn.add_css_class("suggested-action")
        else:
            btn.remove_css_class("suggested-action")
        self.live.sync_generator_button(active, self.live_on)
        self.live.apply()
        self.spectrum.reset_average()

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
        self.live_btn.set_label("АУДИОПОТОК: ЗАКРЫТЬ")
        self.live_btn.add_css_class("suggested-action")
        self.generator_btn.set_sensitive(True)
        self.generator_btn.set_label("▶  ПОДАТЬ ТЕСТОВЫЙ СИГНАЛ")
        self.generator_btn.set_active(False)
        self.live.sync_generator_button(False, True)
        self.live.start()
        if self.stack.get_visible_child() is self.spectrum:
            self.spectrum.set_visible(True)

    def stop_live(self):
        # Сначала снимаем генерацию, затем закрываем оба физических потока.
        if self.generator_btn.get_active():
            self.generator_btn.set_active(False)
        self.generator_btn.set_sensitive(False)
        self.generator_btn.set_label("▶  ПОДАТЬ ТЕСТОВЫЙ СИГНАЛ")
        self.live.sync_generator_button(False, False)
        self.live.stop()
        if self.be is not None:
            self.be.live_stop()
        self.live_on = False
        self.live_btn.set_label("АУДИОПОТОК: ОТКРЫТЬ")
        self.live_btn.remove_css_class("suggested-action")
        self.spectrum.reset_average()
        if self.spectrum.timer is not None:
            GLib.source_remove(self.spectrum.timer)
            self.spectrum.timer = None
        if self.live_btn.get_active():
            self.live_btn.set_active(False)

    # ---- фоновые задачи
    def run_job(self, fn, done, msg="Измерение…"):
        # Keep the status on the page that launched the measurement. It remains
        # visible after completion; a disappearing header spinner was ambiguous.
        page = self.stack.get_visible_child()
        if self.busy:
            self.toast("Другое измерение ещё выполняется")
            if hasattr(page, "set_job_status"):
                page.set_job_status("error", "другое измерение ещё выполняется")
            return
        resume = self.live_on
        if resume:
            self.stop_live()
        if hasattr(page, "set_job_status"):
            page.set_job_status("running", "открываю аудиоустройство")
        if hasattr(page, "set_job_busy"):
            page.set_job_busy(True)
        try:
            rig = self.get_rig()  # GTK/device selections are read on the UI thread.
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Не удалось открыть аудиоустройство: {exc}")
            self.status.set_text("Ошибка устройства")
            if hasattr(page, "set_job_busy"):
                page.set_job_busy(False)
            if hasattr(page, "set_job_status"):
                page.set_job_status("error", f"не удалось открыть устройство: {exc}")
            if resume:
                self.live_btn.set_active(True)
            return
        self.busy = True
        started = time.monotonic()
        self.status.set_text("Измерение…")
        self.spinner.start()
        if hasattr(page, "set_job_status"):
            page.set_job_status("running", msg)

        def finish(res, err):
            elapsed = time.monotonic() - started
            self.busy = False
            self.spinner.stop()
            # Restore previous button states before the result callback: a
            # successful callback may enable the next workflow step (e.g. DUT
            # measurement after loopback calibration).
            if hasattr(page, "set_job_busy"):
                page.set_job_busy(False)
            if err is None:
                try:
                    done(res)
                except Exception as exc:  # noqa: BLE001
                    err = exc
            if err is not None:
                self.toast(f"Ошибка измерения: {err}")
                self.status.set_text("Ошибка измерения")
                if hasattr(page, "say"):
                    page.say(f"Измерение не выполнено:\n{type(err).__name__}: {err}")
                elif hasattr(page, "series_result_label"):
                    page.series_result_label.set_text(f"Измерение не выполнено:\n{type(err).__name__}: {err}")
                if hasattr(page, "set_job_status"):
                    page.set_job_status("error", f"{type(err).__name__}: {err}")
            else:
                self.status.set_text(f"Готово · {elapsed:.1f} с")
                if hasattr(page, "set_job_status"):
                    page.set_job_status("success", "", elapsed)
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
        self.run_job(m.clock_check, lambda r: self.stack.get_visible_child().say(m.text_clock(r)),
                     "проверяю согласование тактов ЦАП и АЦП")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    def on_close(self, *_):
        self.stop_live()
        calib.save(self.cal)
        preferences.save(self.ui_prefs)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.valvebench")
        self.connect("activate", lambda a: Window(a).present())


def main():
    return App().run(sys.argv)
