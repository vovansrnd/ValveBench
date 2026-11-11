#!/usr/bin/env python3
"""Приёмка GUI dev4, задание §2: раздельные селекторы ЦАП/АЦП, парные профили.

Фазы (реальный стол, ручной запуск приложения, одно окно через App._window):
  phase1  миграция старого config; маркерные напряжения в профиль E-MU/E-MU;
          переключение ЦАП на DLX (разделённая пара) — новый pair-key,
          свежий профиль без наследования (пункт 4);
  phase2  перезапуск: выборки восстановлены (пункт 2); возврат АЦП на E-MU —
          прежний профиль пары восстановлен с маркером (пункт 5);
          список Fs = пересечение объявленных (пункт 3).

Каждая фаза — отдельный запуск приложения. Вывод: JSON + текст в stdout.
Конфиг пользователя бэкапится и восстанавливается вызывающим скриптом.
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

PHASE = sys.argv[1] if len(sys.argv) > 1 else "phase1"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "gui-accept-dev4.json")
EMU = "hw:2,0"
DLX = "hw:3,0"
results = []


def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("\n      " + detail if detail else ""), flush=True)


def hw_index(devices, hw):
    for i, (h, _label) in enumerate(devices):
        if h == hw:
            return i
    return -1


def wait_window(app):
    if app._window is None:
        return True
    GLib.timeout_add(600, run_phase)
    return False


def run_phase():
    win = app._window
    cal = win.cal

    if PHASE == "phase1":
        # миграция: config был старого формата (только device=hw:2,0)
        dac_sel = win.selected_dac_hw()
        adc_sel = win.selected_adc_hw()
        check("§2.1a миграция старого config в GUI: DAC=ADC=hw:2,0, не SIM",
              dac_sel == EMU and adc_sel == EMU,
              f"dac={dac_sel} adc={adc_sel} pair_key={win.audio_pair_key()[:44]}")
        old_pair_key = win.audio_pair_key()
        # маркерные напряжения в профиль текущей пары (E-MU/E-MU)
        cal["out_fs_vrms"] = [1.234, 2.345]
        # переключение ЦАП на DLX → разделённая пара
        win.dac_dev.set_selected(hw_index(win.dac_devices, DLX))
        win.adc_dev.set_selected(hw_index(win.adc_devices, EMU))

        def after_switch():
            new_key = win.audio_pair_key()
            check("§2.1b ЦАП и АЦП выбираются независимо",
                  win.selected_dac_hw() == DLX and win.selected_adc_hw() == EMU,
                  f"dac={win.selected_dac_hw()} adc={win.selected_adc_hw()}")
            check("§2.4 разделённая пара: новый pair-key",
                  new_key.startswith("pair:") and new_key != old_pair_key,
                  f"new={new_key[:44]} old={old_pair_key[:44]}")
            prof = cal.d
            check("§2.4 профиль новой пары без наследования калибровки",
                  prof["out_fs_vrms"] != [1.234, 2.345],
                  f"out_fs_vrms новой пары = {prof['out_fs_vrms']} "
                  f"(маркер был [1.234, 2.345])")
            # пункт 3: список Fs = пересечение объявленных
            rates = list(win.rate_values)
            check("§2.3 частоты пары = пересечение объявленных (192k есть, 384k нет)",
                  192000 in rates and 384000 not in rates and 48000 in rates,
                  f"rate_values={rates}")
            GLib.timeout_add(300, app.quit)
            return False
        GLib.timeout_add(1500, after_switch)
        return False

    if PHASE == "phase2":
        dac_sel = win.selected_dac_hw()
        adc_sel = win.selected_adc_hw()
        key = win.audio_pair_key()
        check("§2.2 выборки ЦАП/АЦП пережили перезапуск",
              dac_sel == DLX and adc_sel == EMU and key.startswith("pair:"),
              f"dac={dac_sel} adc={adc_sel} key={key[:44]}")
        # возврат ЦАП на E-MU → прежняя same-device пара E-MU/E-MU
        # (АЦП уже E-MU; пара меняется сменой ЦАП)
        win.dac_dev.set_selected(hw_index(win.dac_devices, EMU))

        def after_revert():
            key2 = win.audio_pair_key()
            vrms = win.cal["out_fs_vrms"]
            check("§2.5 возврат: pair-key прежней пары восстановлен",
                  key2 == _emu_legacy_key(win),
                  f"key={key2[:44]}")
            check("§2.5 профиль прежней пары восстановлен (маркер на месте)",
                  vrms == [1.234, 2.345],
                  f"out_fs_vrms={vrms}")
            GLib.timeout_add(300, app.quit)
            return False
        GLib.timeout_add(1500, after_revert)
        return False


def _emu_legacy_key(win):
    from valvebench.backend import stable_device_identity
    return stable_device_identity(EMU)


def main():
    Adw.init()
    global app
    app = ui.App()
    GLib.timeout_add(200, lambda: wait_window(app) or False)
    app.run([sys.argv[0]])
    json.dump({"phase": PHASE, "results": results},
              open(OUT + "." + PHASE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    n_bad = sum(1 for r in results if not r["ok"])
    print(f"ФАЗА {PHASE}: {len(results) - n_bad}/{len(results)}", flush=True)
    sys.exit(0 if n_bad == 0 else 1)


if __name__ == "__main__":
    main()
