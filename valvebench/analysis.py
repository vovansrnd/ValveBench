"""Фоновый расчёт живого спектра, THD-панели и контроля целостности (0.1.20).

Зачем отдельный процесс. На 192 кГц один кадр живого экрана — два БПФ по 262 144 отсчётов,
анализ тона и проверка целостности — десятки миллисекунд чистого numpy в потоке GUI. Потоки
захвата и воспроизведения (чтение pipe arecord, запись в aplay) живут в том же процессе и
делят с GUI один GIL: пока GUI считает, они не получают управления, а запас до XRUN
ограничен буферами (десятки–сотни мс). Вынос расчёта в дочерний процесс снимает эту связь;
в GUI остаются только усреднение и рисование.

Запасной вариант — поток, если процесс запустить не удалось.
"""
from __future__ import annotations

import concurrent.futures as cf
import multiprocessing as mp
import time

import numpy as np


def spectrum_job(x, fs, n, f_hint, driven, nharm=9, mains=50.0, analysis_n=131072,
                 integrity=True, hf_lo=40000.0):
    """Чистая функция расчёта кадра. x — окно (≥ n, 2). Возвращает dict для отрисовки.

    P[c] — мощность бина (RMS², окно Блэкмана–Харриса, float32); lines = c·P по амплитуде
    синусоидальной линии пересчитывается в GUI (константа ``line_scale``)."""
    from . import dsp, livemetrics as lm

    t0 = time.perf_counter()
    x = np.asarray(x, dtype=np.float64)
    seg = x[-n:]
    w = dsp.bh(n)
    out = {"n": int(n), "fs": float(fs), "P": [], "analysis": [], "integrity": None,
           "line_scale": float(2.0 * n * np.sum(w * w) / np.sum(w) ** 2)}
    for c in (0, 1):
        out["P"].append(dsp.Block(seg[:, c], fs).P.astype(np.float32))
    na = int(min(analysis_n, n))
    tail = seg[-na:]
    for c in (0, 1):
        try:
            hint = f_hint if (f_hint and driven[c]) else None
            out["analysis"].append(lm.analyze_live_tone(tail[:, c], fs, hint, nharm=nharm, mains=mains))
        except Exception as exc:  # noqa: BLE001 — панель не должна ронять кадр
            out["analysis"].append({"ok": False, "reason": f"{type(exc).__name__}: {exc}"})
    if integrity:
        out["integrity"] = lm.frame_integrity(seg, fs, f_hint, tuple(bool(d) for d in driven),
                                              hf_lo=hf_lo)
    out["elapsed"] = time.perf_counter() - t0
    return out


class AnalysisWorker:
    """Не более одного задания в полёте; результат забирается из таймера GUI без блокировки."""

    def __init__(self):
        self._pool = None
        self._fut = None
        self._tag = None
        self.mode = "process"
        self.last_error = ""

    def _ensure(self):
        if self._pool is not None:
            return
        try:
            # spawn, а не fork: в родителе загружены GTK и потоки ALSA — fork небезопасен.
            self._pool = cf.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
            self.mode = "process"
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._pool = cf.ThreadPoolExecutor(max_workers=1)
            self.mode = "thread"

    def busy(self):
        return self._fut is not None and not self._fut.done()

    def submit(self, fn, *args, tag=None, **kwargs):
        """False, если предыдущее задание ещё считается (кадр пропускается, а не копится)."""
        if self.busy():
            return False
        self._ensure()
        try:
            self._fut = self._pool.submit(fn, *args, **kwargs)
            self._tag = tag
        except Exception as exc:  # noqa: BLE001 — пул сломан: перейти на поток
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._pool = cf.ThreadPoolExecutor(max_workers=1)
            self.mode = "thread"
            self._fut = self._pool.submit(fn, *args, **kwargs)
            self._tag = tag
        return True

    def poll(self):
        """(tag, result, error) завершённого задания или None."""
        fut = self._fut
        if fut is None or not fut.done():
            return None
        self._fut = None
        try:
            return self._tag, fut.result(), ""
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, cf.process.BrokenProcessPool):
                # Процесс умер (OOM, сигнал): дальше работаем потоком, чтобы экран не замёрз.
                self._pool = cf.ThreadPoolExecutor(max_workers=1)
                self.mode = "thread"
            return self._tag, None, self.last_error

    def warm_up(self):
        """Запустить процесс заранее: импорт numpy/scipy в дочернем процессе занимает ~1 с."""
        self._ensure()
        try:
            self._pool.submit(_noop)
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass


def _noop():
    import numpy  # noqa: F401
    import scipy.signal  # noqa: F401
    return True
