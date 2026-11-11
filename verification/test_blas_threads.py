#!/usr/bin/env python3
"""Контрольная приёмка fix3, пункт 2: фактическое ограничение BLAS одним потоком.

Проверяется не содержимое os.environ, а СОСТОЯНИЕ БИБЛИОТЕКИ:
openblas_get_num_threads() через ctypes по фактически загруженной
libopenblas из /proc/self/maps, плюс число потоков процесса.

Режимы:
  A. обычный импорт пакета (valvebench раньше numpy)      — ожидание 1
  B. numpy раньше пакета (встраивание)                    — было 4, усиление → 1
  C. заранее установленный OPENBLAS_NUM_THREADS=4         — уважается: 4 (документ)
  D. заранее установленный OPENBLAS_NUM_THREADS=1         — 1
Способы запуска (все идут через пакетный __init__):
  python3 -c "import valvebench.gainmatch"; python3 -m valvebench selftest;
  python3 -m valvebench debug; ./run.sh --version; прямой импорт
  valvebench.ui (GUI-модуль).
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROBE = r"""
import json, re, ctypes, sys
def blas_state():
    try:
        libs = sorted(set(re.findall(r"(/\S+libopenblas\S*\.so\S*)",
                                     open("/proc/self/maps").read())))
    except OSError:
        libs = []
    n = None
    if libs:
        lib = ctypes.CDLL(libs[0])
        for nm in ("openblas_get_num_threads", "openblas_get_num_threads64_"):
            fn = getattr(lib, nm, None)
            if fn:
                fn.restype = ctypes.c_int
                n = fn()
                break
    threads = int([l for l in open("/proc/self/status")
                   if l.startswith("Threads")][0].split()[1])
    return {"blas_threads": n, "proc_threads": threads,
            "blas_lib": libs[0] if libs else None,
            "env": {k: __import__("os").environ.get(k)
                    for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")}}
out = {}
mode = sys.argv[1]
if mode == "valvebench-first":
    import valvebench.gainmatch  # noqa
    import numpy  # noqa
    out = blas_state()
elif mode == "numpy-first":
    import numpy  # noqa
    out["before_valvebench"] = blas_state()
    import valvebench.gainmatch  # noqa
    out["after_valvebench"] = blas_state()
elif mode == "numpy-first-ui":
    import numpy  # noqa
    sys.path.insert(0, %(root)r)
    import valvebench.ui  # noqa
    out["after_valvebench_ui"] = blas_state()
print("PROBE_JSON:" + json.dumps(out))
"""


def probe(mode, env_extra=None, root=ROOT, python_args=None, cwd=None):
    env = dict(os.environ)
    for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    code = PROBE % {"root": root}
    p = subprocess.run([sys.executable, "-c", code, mode] + (python_args or []),
                       capture_output=True, text=True, env=env,
                       cwd=cwd or ROOT, timeout=300)
    for line in p.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:"):]), p
    return None, p


def blas1(x):
    return x is None or x.get("blas_threads") in (None, 1)


def main():
    results = []

    def check(name, ok, detail):
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + "\n      " + detail)

    # A. обычный импорт: пакет раньше numpy
    st, p = probe("valvebench-first")
    check("A: импорт пакета (valvebench раньше numpy) → BLAS=1 поток",
          st and st["blas_threads"] == 1,
          f"blas_threads={st and st['blas_threads']}, proc_threads={st and st['proc_threads']}, "
          f"lib={st and st['blas_lib']}")

    # B. numpy раньше пакета: усиление через ctypes
    st, p = probe("numpy-first")
    check("B: numpy импортирован раньше пакета → было N, после пакета 1",
          st and st["before_valvebench"]["blas_threads"] not in (None, 1)
          and blas1(st["after_valvebench"]),
          f"до: blas={st['before_valvebench']['blas_threads']} "
          f"(потоков {st['before_valvebench']['proc_threads']}), "
          f"после: blas={st['after_valvebench']['blas_threads']}")

    # C. пользователь задал 4 явно — уважается (документированное переопределение)
    st, _ = probe("valvebench-first", {"OPENBLAS_NUM_THREADS": "4"})
    check("C: заранее установленный OPENBLAS_NUM_THREADS=4 уважается",
          st and st["blas_threads"] == 4
          and st["env"]["OPENBLAS_NUM_THREADS"] == "4",
          f"blas_threads={st and st['blas_threads']}, env={st and st['env']}")

    # D. пользователь задал 1 явно
    st, _ = probe("valvebench-first", {"OPENBLAS_NUM_THREADS": "1"})
    check("D: заранее установленный OPENBLAS_NUM_THREADS=1 — 1", st and st["blas_threads"] == 1,
          f"blas_threads={st and st['blas_threads']}")

    # E. запуск python -m valvebench selftest (тот же путь, что GUI: __init__ → main)
    env = dict(os.environ)
    for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, "-m", "valvebench", "selftest"],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=600)
    n_pass = sum(1 for l in p.stdout.splitlines() if l.startswith("PASS"))
    check("E: python -m valvebench selftest проходит (пакетный путь цел)",
          p.returncode == 0 and n_pass >= 90, f"selftest PASS: {n_pass}")

    # F. run.sh --version (продакшн-лаунчер)
    p = subprocess.run([os.path.join(ROOT, "run.sh"), "--version"],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    check("F: run.sh --version", p.returncode == 0 and "0.1.20" in p.stdout,
          p.stdout.strip())

    # G. импорт GUI-модуля при numpy-first (самый косвенный путь)
    st, p = probe("numpy-first-ui")
    check("G: numpy раньше пакета + import valvebench.ui → BLAS=1",
          blas1(st and st.get("after_valvebench_ui")),
          f"{st and st.get('after_valvebench_ui')}"
          + ("" if p.returncode == 0 else f"; stderr: {p.stderr[-200:]}"))

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\nИТОГ: {npass}/{len(results)}")
    with open(os.path.join(HERE, "blas-threads-results.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"results": results}, fh, ensure_ascii=False, indent=1)
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
