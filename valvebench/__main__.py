"""Запуск ValveBench: GUI, CLI, диагностика и self-test."""
from __future__ import annotations

import argparse
import sys

from . import __version__


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(f"ValveBench {__version__}")
        return 0
    if argv and argv[0] == "selftest":
        from .selftest import run_selftest, format_selftest
        result = run_selftest()
        print(format_selftest(result))
        return 0 if result["passed"] == result["total"] else 1
    if argv and argv[0] == "debug":
        ap = argparse.ArgumentParser(prog="valvebench debug")
        ap.add_argument("--no-selftest", action="store_true", help="не запускать DSP self-test")
        ap.add_argument("--save", action="store_true", help="сохранить отчёт в ~/.local/state/valvebench/debug/")
        args = ap.parse_args(argv[1:])
        from .diagnostics import make_report, save_report
        report = make_report(include_selftest=not args.no_selftest)
        print(report)
        if args.save:
            print("\nREPORT_SAVED:", save_report(report))
        return 0
    if argv and argv[0] == "cli":
        from .cli import main as cli_main
        return cli_main(argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print("""ValveBench — измерительная лаборатория

Использование:
  ./valvebench                 открыть GTK-интерфейс
  ./valvebench selftest        проверки DSP и симулятора без аудиокарты
  ./valvebench debug           текстовый отчёт о системе + self-test
  ./valvebench debug --save    то же, с сохранением отчёта в файл
  ./valvebench cli --help      консольные измерительные приборы

Начните с ./valvebench selftest, затем ./valvebench debug.
""")
        return 0
    from .ui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
