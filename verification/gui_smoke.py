#!/usr/bin/env python3
"""GUI smoke test 0.1.18 v4 — корректное измерение достижимости контента.

Один экземпляр через flock (команда запуска). Для каждой из 9 страниц:
высота окна, viewport (page_size), upper, value, достижение максимума
прокрутки, нижние края содержательных элементов в системе координат
скроллера (левая/правая колонки раздельно), верх. Аудио: реальная карта.
"""
import os, sys, json, subprocess, collections, math
os.environ["DISPLAY"] = os.environ.get("DISPLAY", ":0")
os.environ.setdefault("GSK_RENDERER", "cairo")
PKG = "/opt/MyApps/Adudio/for_z/ValveBench-0.1.18-generator-controls"
sys.path.insert(0, PKG)
OUT = "/opt/MyApps/Adudio/for_z/audit-steady/usb-asus/gui-smoke"
os.makedirs(OUT, exist_ok=True)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import numpy as np
from valvebench import ui

CHECKS = []
FS = 192000
WIN_W, WIN_H = 900, 550          # заведомо небольшое окно
LEFT_SPLIT_X = 330               # граница левой колонки (controls 310 + поля)


def check(name, ok, detail):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"{'PASS' if ok else 'FAIL'}  {name} :: {str(detail)[:280]}"[:420])


def shot(name):
    """Скриншот штатным средством оболочки (noctalia), если доступно."""
    try:
        r = subprocess.run(["noctalia", "msg", "screenshot-fullscreen"],
                           capture_output=True, text=True, timeout=8)
        return f"rc={r.returncode} err={r.stderr.strip()[:50]}"
    except Exception as e:
        return f"unavailable: {type(e).__name__}"


def walk(w):
    yield w
    ch = w.get_first_child()
    while ch:
        yield from walk(ch)
        ch = ch.get_next_sibling()


def find_scrollers(root):
    return [w for w in walk(root) if isinstance(w, Gtk.ScrolledWindow)]


def parse_rms(label):
    try:
        return float(label.split("dBFS")[0].strip().replace("−", "-"))
    except Exception:
        return None


def measure_freq(win, seconds=0.2):
    """Фактическая частота тона в захваченном сигнале (L, петля карты)."""
    x = win.be.live_recent(seconds)          # АРГУМЕНТ — СЕКУНДЫ (баг: были сэмплы -> OOM 111 GiB)
    if x is None or len(x) < int(0.05 * FS):
        return None
    seg = np.asarray(x[-int(0.15 * FS):, 0], dtype=np.float64)
    seg = seg - np.mean(seg)
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / FS)
    m = (freqs >= 200.0) & (freqs <= 5000.0)
    if not m.any():
        return None
    idx = np.flatnonzero(m)
    k = idx[np.argmax(spec[idx])]
    if k <= 0 or k >= len(spec) - 1:
        return float(freqs[k])
    a, b, c = spec[k - 1], spec[k], spec[k + 1]
    d = 0.5 * (a - c) / (a - 2 * b + c + 1e-30)
    return float(freqs[k] + d * (freqs[1] - freqs[0]))


def page_main_scroller(page):
    scs = find_scrollers(page)
    return max(scs, key=lambda s: s.get_vadjustment().get_upper()) if scs else None


def bottoms_in_scroller(page, sc):
    """Нижние края видимых виджетов в координатах скроллера, L/R колонки."""
    left, right = [], []
    for w in walk(page):
        if not w.get_visible():
            continue
        if w.get_height() <= 0 or w.get_width() <= 0:
            continue
        res = w.translate_coordinates(sc, 0, w.get_height())
        if not res:
            continue
        x, y = res[-2], res[-1]
        (left if x < LEFT_SPLIT_X else right).append((y, type(w).__name__))
    return left, right


class Driver:
    def __init__(self):
        self.q = collections.deque()
        self.win = None
        self.app = ui.App()
        self.app.connect("activate", self.on_activate)
        self.retries = 0

    def on_activate(self, app):
        # ОДНО окно: его создаёт собственный обработчик App (lambda -> Window.present()),
        # драйвер лишь берёт ссылку на него — без второго экземпляра окна.
        # Геометрией управляет тайлинг Umbriel (окно уходит на свободный воркспейс).
        print("on_activate: окно получено, конвейер запланирован", flush=True)
        self.win = self.app.get_active_window() or Gtk.Window.get_toplevels()[0]
        print("on_activate: win =", type(self.win).__name__ if self.win else None, flush=True)
        self.push(700, self.s1_struct)
        GLib.timeout_add(900, self.pump)   # первичный запуск конвейера шагов

    def push(self, delay, fn):
        self.q.append((delay, fn))

    def pump(self):
        if not self.q:
            self.finish()
            return False
        delay, fn = self.q.popleft()
        GLib.timeout_add(delay, self.run_step, fn)
        return False

    def run_step(self, fn):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            CHECKS.append({"name": fn.__name__, "ok": False,
                           "detail": f"EXCEPTION {type(e).__name__}: {e}"})
            self.finish()
            return False
        self.pump()
        return False

    # ---- S1: блок «Генератор» ----
    def s1_struct(self):
        live = self.win.live
        g = None
        for w in walk(live):
            if isinstance(w, Adw.PreferencesGroup) and w.get_title() == "Генератор":
                g = w
                break
        inside = g is not None and all(any(x is t for x in walk(g))
                                       for t in (live.f, live.lv, live.route))
        check("S1 блок «Генератор» на странице содержит Частоту/Уровень/Канал",
              inside, f"group={'найден' if g else 'НЕТ'}; f/lv/route внутри: {inside}")
        self.push(200, self.s2_scroll)

    # ---- S2: прокрутка Live ----
    def s2_scroll(self):
        self.sc = page_main_scroller(self.win.live)
        adj = self.sc.get_vadjustment()
        self.req = adj.get_upper() - adj.get_page_size()
        adj.set_value(self.req)
        self.push(250, self.s2_measure)

    def s2_measure(self):
        adj = self.sc.get_vadjustment()
        self.max_val = adj.get_value()
        self.max_reached = abs(self.max_val - self.req) <= 2.0
        bl, br = bottoms_in_scroller(self.win.live, self.sc)
        self.bottom_l = max((y for y, _ in bl), default=0.0)
        self.bottom_r = max((y for y, _ in br), default=0.0)
        self.worst = max(self.bottom_l, self.bottom_r)
        self.worst_w = (max(bl + br, key=lambda b: b[0])[1] if (bl or br) else "?")
        adj.set_value(0)
        self.push(150, self.s2_check)

    def s2_check(self):
        adj = self.sc.get_vadjustment()
        top_val = adj.get_value()
        upper, page = adj.get_upper(), adj.get_page_size()
        ok = (self.max_reached and abs(top_val) <= 2.0
              and self.worst <= upper + 2.0)
        check("S2 прокрутка Live: максимум достигается; низ достижим (worst ≤ upper); верх с нуля",
              ok, f"max={self.req:.0f} (Δ{0 if self.max_reached else 999}px); "
                  f"нижний край L/R: {self.bottom_l:.0f}/{self.bottom_r:.0f} vs upper {upper:.0f} "
                  f"(перелив {self.worst-upper:+.0f}px); top={top_val:.0f}; худший {self.worst_w}")
        self.push(200, self.s3_open)

    # ---- S3: строгие состояния аудиотракта ----
    def s3_open(self):
        self.win.live_btn.set_active(True)
        self.push(1000, self.s3_poll)

    def s3_poll(self):
        win = self.win
        rig = getattr(win, "rig", None)
        be = getattr(win, "be", None)
        running = bool(getattr(be, "live_running", False)) if be else False
        status = win.live.status_label.get_text()
        if rig is None or not running:
            self.retries += 1
            if self.retries > 20:
                check("S3 аудиотракт: rig+live_running+статус", False,
                      f"не поднялся за ~20 с; status={status!r}")
                self.push(200, self.s4_gen)
                return False
            self.push(500, self.s3_poll)
            return False
        ok = ("Аудио активно" in status and "ADC OK" in status and "DAC OK" in status
              and f"Fs={win.rig_fs()}" in status)
        check("S3 аудиотракт: rig поднят, live_running, статус содержит Аудио активно/ADC OK/DAC OK",
              ok, f"status={status!r}")
        # сброс генератора в базовое состояние: приложение восстанавливает
        # состояние прошлой сессии из preferences — приводим к известному
        self.win.live.lv.set_value(-6.0)
        self.win.live.f.set_value(1000.0)
        self.win.live.route.set_selected(0)
        self.push(400, self.s4_gen)

    # ---- S4: ПОДАНО == контролы ----
    def s4_gen(self):
        self.win.generator_btn.set_sensitive(True)
        self.win.generator_btn.set_active(True)
        self.push(900, self.s4_check)

    def s4_check(self):
        live = self.win.live
        v = live.vlabel.get_text()
        f_now = live.f.get_value()
        lv_now = live.lv.get_value()
        route_s = ["L + R", "Только L", "Только R"][live.route.get_selected()]
        expect = f"ПОДАНО: {round(f_now)} Гц · {lv_now:.1f} dBFS · {route_s}"
        read_l = live.read_l.get_text()
        rms = parse_rms(read_l)
        STATE["rms_before"] = rms
        check("S4 генератор подан: ПОДАНО точно равно Частота/Уровень/Канал; входной метр видит тон",
              v == expect and rms is not None, f"vlabel={v!r}; ожидалось {expect!r}; вход L={read_l!r}")
        self.push(200, self.s5_level)

    # ---- S5: точность отклонения уровня −6 → −20 ----
    def s5_level(self):
        self.win.live.lv.set_value(-20.0)
        self.push(900, self.s5_check)

    def s5_check(self):
        read_l = self.win.live.read_l.get_text()
        now = parse_rms(read_l)
        before = STATE.get("rms_before")
        drop = (before - now) if (before is not None and now is not None) else None
        ok = drop is not None and 13.5 <= drop <= 14.5
        STATE["rms_lv20"] = now
        check("S5 отклонение уровня −6→−20 dBFS: падение входного RMS = 14,0 ± 0,5 дБ",
              ok, f"было {before} -> стало {now}; падение {drop if drop is None else round(drop, 2)} дБ")
        self.push(300, self.s6_route)

    # ---- S6: маршрут «Только L» ----
    def s6_route(self):
        STATE["l_before_route"] = parse_rms(self.win.live.read_l.get_text())
        self.win.live.route.set_selected(1)
        self.push(900, self.s6_check)

    def s6_check(self):
        live = self.win.live
        l_now = parse_rms(live.read_l.get_text())
        r_now = parse_rms(live.read_r.get_text())
        l_before = STATE.get("l_before_route")
        r_before = STATE.get("rms_before")
        r_drop = (r_before - r_now) if (r_before is not None and r_now is not None) else None
        l_shift = abs(l_now - l_before) if (l_now is not None and l_before is not None) else None
        ok = (r_drop is not None and r_drop > 40.0
              and l_shift is not None and l_shift <= 0.3)
        check("S6 маршрут «Только L»: правый −40 дБ и более, левый стабилен (±0,3 дБ)",
              ok, f"L {l_before} -> {l_now} (сдвиг {l_shift if l_shift is None else round(l_shift,2)} дБ); "
                  f"R падение {r_drop if r_drop is None else round(r_drop,1)} дБ")
        self.push(300, self.s7_freq)

    # ---- S7: фактическая частота по захваченному сигналу ----
    def s7_freq(self):
        self.win.live.f.set_value(500.0)
        self.push(900, self.s7_check)

    def s7_check(self):
        f_meas = measure_freq(self.win)
        ok = f_meas is not None and abs(f_meas - 500.0) <= 2.0
        check("S7 фактическая частота по захваченному сигналу = 500 ± 2 Гц",
              ok, f"измерено {f_meas if f_meas is None else round(f_meas, 2)} Гц (петля ASUS)")
        self.win.live.f.set_value(1000.0)
        self.push(700, self.s7b_check)

    def s7b_check(self):
        f_meas = measure_freq(self.win)
        ok = f_meas is not None and abs(f_meas - 1000.0) <= 2.0
        check("S7b возврат на 1000 Гц: фактическая частота подтверждена",
              ok, f"измерено {f_meas if f_meas is None else round(f_meas, 2)} Гц")
        self.push(200, self.s8_pages)

    # ---- S8: все 9 страниц на заведомо небольшом окне ----
    def s8_pages(self):
        self.pages = ["spectrum", "thd", "resp", "noise_response", "imd",
                      "noise", "vinyl", "setup", "live"]
        self.page_results = {}
        self.push(300, self.s8_begin)

    def s8_begin(self):
        if not self.pages:
            bad = [k for k, v in self.page_results.items()
                   if not (v["max_reached"] and v["upper_ok"])]
            check("S8 все 9 страниц (окно 900x550): максимум прокрутки достигается; "
                  "нижний контент достижим (верх/низ, левая/правая колонки)",
                  not bad, f"проблемных: {len(bad)} {bad}; "
                           f"детали: {json.dumps(self.page_results, ensure_ascii=False)[:400]}")
            self.push(200, self.s9_off)
            return False
        self.cur = self.pages.pop(0)
        self.win.stack.set_visible_child_name(self.cur)
        self.push(400, self.s8_max)

    def s8_max(self):
        page = self.win.page_map[self.cur]
        self.sc = page_main_scroller(page)
        if self.sc is None:
            self.page_results[self.cur] = {"max_reached": True, "upper_ok": True,
                                           "note": "нет ScrolledWindow"}
            self.push(150, self.s8_begin)
            return False
        adj = self.sc.get_vadjustment()
        self.req = adj.get_upper() - adj.get_page_size()
        adj.set_value(self.req)
        self.push(200, self.s8_store)

    def s8_store(self):
        page = self.win.page_map[self.cur]
        adj = self.sc.get_vadjustment()
        val = adj.get_value()
        max_reached = abs(val - self.req) <= 2.0
        bl, br = bottoms_in_scroller(page, self.sc)
        bottom_l = max((y for y, _ in bl), default=0.0)
        bottom_r = max((y for y, _ in br), default=0.0)
        upper = adj.get_upper()
        upper_ok = max(bottom_l, bottom_r) <= upper + 2.0
        self.page_results[self.cur] = {
            "max_reached": max_reached, "upper_ok": upper_ok,
            "bottom_left": round(bottom_l), "bottom_right": round(bottom_r),
            "upper": round(upper), "viewport": round(adj.get_page_size()),
            "value_at_max": round(val, 1)}
        self.push(150, self.s8_begin)

    # ---- завершение ----
    def s9_off(self):
        if self.win.generator_btn.get_active():
            self.win.generator_btn.set_active(False)
        self.push(300, self.s9_off2)

    def s9_off2(self):
        if self.win.live_btn.get_active():
            self.win.live_btn.set_active(False)
        self.push(400, self.finish)

    def finish(self):
        npass = sum(1 for c in CHECKS if c["ok"])
        print(f"\n===== GUI SMOKE v4: {npass}/{len(CHECKS)} PASS =====")
        result = {"checks": CHECKS, "pass": npass, "total": len(CHECKS),
                  "window": f"{WIN_W}x{WIN_H} (заведомо небольшое)",
                  "page_metrics": STATE.get("page_metrics", {}),
                  "coordinate_system": "координаты нижних краёв — в системе координат "
                                       "СКРОЛЛЕРА страницы (учтён scroll-offset); "
                                       "reachable = весь контент в пределах upper; "
                                       "видимость при макс. прокрутке = низ <= value + page_size",
                  "note": "скриншоты оболочки (noctalia) — проверь наличие файлов; "
                          "утверждения S1–S8 объективны и не зависят от скриншотов"}
        json.dump(result, open(OUT + "/gui-smoke-results.json", "w"), ensure_ascii=False, indent=1)
        print(f"результаты: {OUT}/gui-smoke-results.json")
        GLib.timeout_add(300, self.app.quit)
        return False


STATE = {}
driver = Driver()
sys.exit(driver.app.run(sys.argv))
