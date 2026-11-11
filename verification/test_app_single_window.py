#!/usr/bin/env python3
"""P0 «два окна»: тест жизненного цикла окна App (fix4).

Несколько раз вызывает app.activate() и доказывает:
  1. первое activate создаёт РОВНО одно окно;
  2. повторные activate НЕ создают новых окон (тот же объект Window,
     len(app.get_windows()) == 1);
  3. закрытие окна сбрасывает ссылку (app._window is None), ресурсы
     освобождает штатный Window.on_close (не тронут);
  4. activate после закрытия создаёт одно НОВОЕ окно (не воскресает старое);
  5. счётчик инстанцирований Window == числу ожидаемых созданий (2).

Запуск под дисплеем (xvfb или рабочий стол): test_app_single_window.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

import valvebench.ui as ui  # noqa: E402

STEPS = []
FAILED = []
CREATED = []


def _counting_init(self, app):
    ui._orig_window_init(self, app)
    CREATED.append(self)


def install_counter():
    if not hasattr(ui, "_orig_window_init"):
        ui._orig_window_init = ui.Window.__init__
        ui.Window.__init__ = _counting_init


def check(name, ok, detail=""):
    STEPS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("\n      " + detail if detail else ""))
    if not ok:
        FAILED.append(name)


def main():
    Adw.init()
    install_counter()
    app = ui.App()
    app.register(None)  # стать primary без run(): activate можно вызывать вручную
    app.hold()  # без этого закрытие последнего окна завершает mainloop (шаги 5-7 не дойдут)

    seq = {"step": 0}

    def step1():
        seq["step"] = 1
        app.activate()
        GLib.timeout_add(400, step2)
        return False

    def step2():
        # после первого activate: ровно одно окно
        wins = app.get_windows()
        check("1. первое activate: создано одно окно",
              len(wins) == 1 and len(CREATED) == 1,
              f"get_windows={len(wins)}, создано Window={len(CREATED)}")
        seq["w1"] = app._window
        check("2. ссылка app._window установлена", seq["w1"] is not None
              and isinstance(seq["w1"], ui.Window),
              f"app._window={type(app._window).__name__}")
        app.activate()
        GLib.timeout_add(400, step3)
        return False

    def step3():
        wins = app.get_windows()
        check("3. повторное activate: окон по-прежнему одно, объект тот же",
              len(wins) == 1 and len(CREATED) == 1 and app._window is seq["w1"],
              f"get_windows={len(wins)}, создано Window={len(CREATED)}, "
              f"тот же объект={app._window is seq['w1']}")
        app.activate()
        GLib.timeout_add(400, step4)
        return False

    def step4():
        check("4. третье activate: окон по-прежнему одно",
              len(app.get_windows()) == 1 and len(CREATED) == 1,
              f"get_windows={len(app.get_windows())}, создано Window={len(CREATED)}")
        seq["w1"].close()
        GLib.timeout_add(600, step5)
        return False

    def step5():
        check("5. закрытие окна: ссылка сброшена",
              app._window is None and len(app.get_windows()) == 0,
              f"app._window={app._window}, get_windows={len(app.get_windows())}")
        app.activate()
        GLib.timeout_add(400, step6)
        return False

    def step6():
        wins = app.get_windows()
        check("6. activate после закрытия: одно НОВОЕ окно",
              len(wins) == 1 and len(CREATED) == 2
              and app._window is not seq["w1"],
              f"get_windows={len(wins)}, всего созданий Window={len(CREATED)}, "
              f"новое окно не равно старому={app._window is not seq['w1']}")
        finish()
        return False

    def finish():
        check("7. счётчик инстанцирований Window == 2 (начальное + после закрытия)",
              len(CREATED) == 2, f"создано Window={len(CREATED)}")
        n_pass = sum(1 for _, ok, _ in STEPS if ok)
        print(f"\nИТОГ: {n_pass}/{len(STEPS)}")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "app-single-window-results.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"results": STEPS, "windows_created": len(CREATED)}, fh,
                      ensure_ascii=False, indent=1)
        GLib.timeout_add(200, app.quit)
        return False

    GLib.idle_add(step1)
    app.run(sys.argv)
    sys.exit(0 if not FAILED else 1)


if __name__ == "__main__":
    main()
