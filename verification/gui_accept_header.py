#!/usr/bin/env python3
"""Приёмка GUI dev4, задание §2.6: HeaderBar не меняет размер/подписи селекторов,
когда в шапке появляются предупреждения потока, XRUN и сообщения переподключения.

Изолированный стенд (Xvfb, симулятор — раскладка не зависит от железа):
в течение 200 циклов качаем в шапку длинные сообщения (set_health, set_stale,
refresh_health с длинным статусом, XRUN-плашки) и каждые 10 циклов пишем
аллокации dac_dev/adc_dev и их текст. Успех: аллокации постоянны, подписи
селекторов не изменились. Контроль и «после»: те же замеры в спокойном состоянии.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

import valvebench.ui as ui  # noqa: E402

results = []
samples = []


def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("\n      " + detail if detail else ""), flush=True)


LONG_MSGS = [
    ("bad", "XRUN: захват overrun — потеряно 512 отсчётов, окно недостоверно, повторите измерение"),
    ("bad", "Поток: закрыт"),
    ("warning", "переподключение USB: карта переоткрыта, проверьте частоту дискретизации и маршрут"),
    ("info", "Аудио · Fs=192000 Гц · ADC OK (192000.0 с принято) · DAC OK (192000.0 с отправлено) — длинный статус для проверки переноса"),
    ("bad", "на входе нет тона: проверь кабели и частоту"),
]


def alloc_of(w):
    a = w.get_allocation()
    return (a.x, a.y, a.width, a.height)


def main():
    Adw.init()
    app = ui.App()
    state = {"n": 0, "phase": "idle", "baseline": None, "texts": set()}

    def tick():
        win = app._window
        if win is None:
            return True
        if state["phase"] == "idle":
            # базовые аллокации в спокойном состоянии
            state["baseline"] = (alloc_of(win.dac_dev), alloc_of(win.adc_dev),
                                 win.dac_dev.get_label() if hasattr(win.dac_dev, "get_label") else "")
            state["texts"] = {win.dac_dev.get_selected_item().get_string(),
                              win.adc_dev.get_selected_item().get_string()}
            state["phase"] = "stress"
            state["n"] = 0
            return True
        if state["phase"] == "stress":
            i = state["n"] % len(LONG_MSGS)
            level, msg = LONG_MSGS[i]
            win.strip.set_health(level, msg)
            if i % 2:
                win.strip.meters.set_stale(msg)       # как в продукте: только строки
            else:
                # снятие stale — как в продукте: set_reading по каналам
                for c in (0, 1):
                    win.strip.meters.set_reading(c, -20.0, -12.0)
            fake = {"capture_alive": i % 3 != 0, "playback_alive": True,
                    "capture_stale": i % 3 == 0, "playback_stale": False,
                    "capture_frames": 48000 * (i + 1), "playback_frames": 48000 * (i + 1),
                    "xruns": i, "capture_xruns": i, "playback_xruns": 0}
            try:
                win.refresh_health(fake)
            except Exception:
                pass
            state["n"] += 1
            if state["n"] % 10 == 0:
                samples.append({"cycle": state["n"],
                                "dac_alloc": alloc_of(win.dac_dev),
                                "adc_alloc": alloc_of(win.adc_dev)})
            if state["n"] >= 200:
                state["phase"] = "after"
            return True
        if state["phase"] == "after":
            win.strip.set_health(None, "")
            b = state["baseline"]
            now = (alloc_of(win.dac_dev), alloc_of(win.adc_dev))
            texts_now = {win.dac_dev.get_selected_item().get_string(),
                         win.adc_dev.get_selected_item().get_string()}
            check("§2.6 аллокации селекторов постоянны во время предупреждений/XRUN",
                  all(s["dac_alloc"] == b[0] and s["adc_alloc"] == b[1] for s in samples),
                  f"циклов={len(samples)}·10; базовые dac={b[0]} adc={b[1]}; "
                  f"различий=0" if all(s["dac_alloc"] == b[0] and s["adc_alloc"] == b[1]
                                       for s in samples) else
                  f"изменения: {[s for s in samples if s['dac_alloc'] != b[0] or s['adc_alloc'] != b[1]][:3]}")
            check("§2.6 подписи селекторов не изменились",
                  texts_now == state["texts"], f"до={state['texts']} после={texts_now}")
            check("§2.6 аллокации после снятия предупреждений = базовые",
                  now == (b[0], b[1]), f"после={now} базовые={(b[0], b[1])}")
            json.dump({"samples": samples, "results": results},
                      open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "gui-accept-header.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
            app.quit()
            return False
        return True

    def wait_window():
        if app._window is None:
            return True
        GLib.timeout_add(300, tick)
        return False

    GLib.timeout_add(200, wait_window)
    app.run([sys.argv[0]])
    n_bad = sum(1 for r in results if not r["ok"])
    print(f"ИТОГ: {len(results) - n_bad}/{len(results)}")
    sys.exit(0 if n_bad == 0 else 1)


if __name__ == "__main__":
    main()
