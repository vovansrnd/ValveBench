"""GTK4 / libadwaita интерфейс. Вся логика измерений — в measure.py; здесь только формы,
графики и фоновые потоки (звук никогда не трогается из потока GUI)."""
from __future__ import annotations

import sys
import threading
import time

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import calib, dsp, measure as m, signals, store, vinyl  # noqa: E402
from .gainmatch import GainTracker  # noqa: E402
from .gainview import GainDeltaView  # noqa: E402
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
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                         margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.win = win
        self.f = spin("Частота, Гц", 5, 40000, 1, 1000, 0)
        self.lv = spin("Уровень генератора, dBFS", -100, -0.5, 0.5, -20, 1)
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
        self.tol = spin("Допуск ΔG, дБ", 0.01, 2.0, 0.01, 0.10, 2, "зелёная зона индикатора")
        self._delta_error_shown = False

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        controls.set_size_request(310, -1)
        controls.append(group("Генератор — параметры", self.f, self.lv, self.route, self.tol))
        # Единственная кнопка генератора находится на главном измерительном экране.
        # Это тот же ToggleButton, состояние которого управляет аудиодвижком.
        self.gen_action = self.win.generator_btn
        self.gen_action.set_size_request(-1, 60)
        self.gen_action.set_hexpand(True)
        self.gen_action.add_css_class("suggested-action")
        controls.append(self.gen_action)
        hint = Gtk.Label(
            label="Порядок работы: 1) включи «АУДИО ВКЛ» в верхней панели; "
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
            label="Напряжения и усиление появятся после раздельной калибровки L/R. Для входа MM необходим "
                  "внешний аттенюатор и корректное сопротивление источника.", xalign=0, wrap=True))
        self.append(controls)

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
        self.append(right)

        for w in (self.f, self.lv):
            w.connect("notify::value", lambda *_: self.apply())
        self.route.connect("notify::selected", lambda *_: self.apply())

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
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.win = win
        self.fft = Gtk.DropDown.new_from_strings([
            "8k · самый быстрый отклик",
            "16k · быстро",
            "32k · среднее разрешение",
            "64k · повышенное разрешение",
            "128k · высокое разрешение",
            "256k · максимальное разрешение"])
        self.fft.set_selected(0)
        self.mode = Gtk.DropDown.new_from_strings([
            "Амплитуда линий · dBFS/bin", "Плотность шума · dBFS/√Гц"])
        # PSD view is the most useful default for seeing the continuous noise floor.
        self.mode.set_selected(1)
        self.floor = Gtk.DropDown.new_from_strings(["−120", "−140", "−160", "−180", "−200"])
        self.floor.set_selected(2)
        self.top = Gtk.DropDown.new_from_strings(["0", "−20", "−40", "−60", "−80"])
        self.top.set_selected(0)
        self.band = Gtk.DropDown.new_from_strings(["20 Гц – 20 кГц", "10 Гц – 40 кГц"])
        self.band.set_selected(0)
        self.avg_mode = Gtk.DropDown.new_from_strings(["Без усреднения", "Быстрое", "Сглаженное"])
        self.avg_mode.set_selected(1)
        self.state = Gtk.Label(label="Аудиопоток выключен", xalign=0)
        self.state.add_css_class("dim-label")
        self.plot = Plot(True, "Частота, Гц", "Уровень, dBFS/bin")
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
        self.append(toolbar)
        self.append(self.plot)
        self.append(self.state)
        help_label = Gtk.Label(
            label="Амплитуда линий: dBFS/bin, уровень синусоидальной составляющей в каждом бине. "
                  "Плотность шума: dBFS/√Гц, позволяет сравнивать шумовую полку при разном FFT. "
                  "Δf=Fs/N; ENBW окна Блэкмана–Харриса около 2·Δf. Уровень тона и плотность шума — разные величины.",
            xalign=0, wrap=True)
        help_label.add_css_class("dim-label")
        self.append(help_label)
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
                coeff_bin = np.where(bin_power >= prev_bin, attack, release)
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
                ylabel = "Плотность шума, dBFS/√Гц"
            traces.append({"x": block.freq[1:], "y": y[1:], "label": label, "color": label, "width": 1.7})
        self.plot.xlog = True
        xlo = 20.0 if self.band.get_selected() == 0 else 10.0
        ylo = float([-120, -140, -160, -180, -200][self.floor.get_selected()])
        yhi = float([0, -20, -40, -60, -80][self.top.get_selected()])
        if ylo >= yhi:
            ylo, yhi = (-160.0, 0.0) if self.mode.get_selected() == 0 else (-200.0, -60.0)
        self.plot.set_view((xlo, min(20000.0 if self.band.get_selected() == 0 else 40000.0, 0.45 * fs)),
                           (ylo, yhi), xlabel="Частота, Гц", ylabel=ylabel)
        if self.plot.ylim[0] >= self.plot.ylim[1]:
            self.plot.set_view(ylim=(-160, 0))
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
        self.state.set_text(f"{source} · {mode_name} · N={n:,} · Δf={df:.3f} Гц · ENBW={enbw:.2f} Гц · "
                            f"окно {n/fs:.3f} с · обновление ≥{refresh_ms} мс · {xrun_text} · "
                            f"ADC frames={status.get('capture_frames', 0):,}")
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
        super().__init__(application=app, title="ValveBench", default_width=1320, default_height=760)
        self.cal = calib.load()
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
        sel = next((i for i, (d, _) in enumerate(self.cards) if d == saved_device), 0)
        if self.cards[sel][0] != saved_device:
            self.cal["device"] = "sim"
            self.invalid_saved_device = saved_device
        else:
            self.invalid_saved_device = None
        self.dev.set_selected(sel)
        self.rate = Gtk.DropDown.new_from_strings([f"{r / 1000:g} кГц" for r in RATES])
        self.rate.set_selected(RATES.index(self.cal["fs"]) if self.cal["fs"] in RATES else 3)
        self.live_btn = Gtk.ToggleButton(label="АУДИО ВЫКЛ")
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
        header.pack_start(button("Такт", self.check_clock))
        header.pack_end(self.spinner)
        header.pack_end(self.status)
        self.dev.connect("notify::selected", self.on_device_changed)
        self.rate.connect("notify::selected", self.on_rate_changed)

        self.stack = Gtk.Stack()
        self.live = LivePage(self)
        self.spectrum = SpectrumPage(self)
        self.noise_response_page = NoiseResponsePage(self)
        pages = (
            ("live", "Gain Match / Уровень", self.live),
            ("spectrum", "Спектр (большой экран)", self.spectrum),
            ("thd", "THD / THD+N", ThdPage(self)),
            ("resp", "АЧХ / RIAA / Sweep", ResponsePage(self)),
            ("noise_response", "АЧХ шумом / H1", self.noise_response_page),
            ("imd", "IMD", ImdPage(self)),
            ("noise", "Шум / Crosstalk", NoisePage(self)),
            ("vinyl", "TACET / Vinyl Test", VinylPage(self)),
            ("setup", "Калибровка / Setup", SetupPage(self)),
        )
        for name, title, page in pages:
            self.stack.add_titled(page, name, title)
        side = Gtk.StackSidebar(stack=self.stack)
        side.set_size_request(200, -1)
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
            self.toast(f"Сохранённое устройство {self.invalid_saved_device} не найдено. Выбран симулятор; выбери нужную карту вверху.")

    def on_device_changed(self, *_):
        dev, _label = self.cards[self.dev.get_selected()]
        self.cal["device"] = dev
        self.reset_rig()
        # Сохраняем выбор сразу: внешний debug-процесс должен видеть
        # текущее устройство, даже пока GUI остаётся открытым.
        calib.save(self.cal)

    def on_rate_changed(self, *_):
        self.reset_rig()
        # Частота также является частью конфигурации аппаратного потока.
        calib.save(self.cal)

    def on_page_changed(self, *_):
        page = self.stack.get_visible_child()
        if page is not self.noise_response_page and self.noise_response_page.live_toggle.get_active():
            self.noise_response_page.live_toggle.set_active(False)
        self.spectrum.set_visible(page is self.spectrum)

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

    def on_generator_toggle(self, btn):
        active = btn.get_active()
        if active and not self.live_on:
            btn.set_active(False)
            self.toast("Сначала включи «АУДИО ВКЛ»: это открывает физические ЦАП и АЦП.")
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
        self.live_btn.set_label("АУДИО ВКЛ")
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
        self.live_btn.set_label("АУДИО ВЫКЛ")
        self.live_btn.remove_css_class("suggested-action")
        self.spectrum.reset_average()
        if self.spectrum.timer is not None:
            GLib.source_remove(self.spectrum.timer)
            self.spectrum.timer = None
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
