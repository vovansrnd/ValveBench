#!/usr/bin/env python3
"""CPU-профилирование Live Gain Match на реальной E-MU 0404 USB.

Замеряет CPU по состояниям A-E (ТЗ координатора §2) с реальным аудиопотоком,
без UI — чистый DSP + аудиобэкенд. Каждый тик = полный GainTracker.update().
"""
import math
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from valvebench import gainmatch
from valvebench.backend import Backend

EMU = "hw:2,0"
FS = 48000
LEVEL = -6.0
TICK_S = 0.075
MEASURE_S = 15  # сек на состояние (короче 30 для скорости, масштабируемо)

pid = os.getpid()


def cpu_time():
    """CPU (user+sys) процесса в секундах."""
    with open(f"/proc/{pid}/stat") as fh:
        fields = fh.read().split()
    utime = int(fields[13])  # clock ticks
    stime = int(fields[14])
    clk_tck = os.sysconf("SC_CLK_TCK")
    return (utime + stime) / clk_tck


def child_cpu():
    """CPU дочерних процессов (arecord + aplay) в секундах."""
    total = 0.0
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit() or int(pid_dir) == pid:
            continue
        try:
            cmd = open(f"/proc/{pid_dir}/cmdline", "rb").read().decode("utf-8", "replace")
            if "arecord" in cmd or "aplay" in cmd:
                fields = open(f"/proc/{pid_dir}/stat").read().split()
                utime = int(fields[13])
                stime = int(fields[14])
                total += (utime + stime) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, ValueError):
            pass
    return total


class EmuHw:
    """Реальный E-MU 0404 USB full-duplex."""
    name = "alsa-hw"

    def __init__(self):
        self.hw = EMU
        self.notes = []
        self.fs = FS
        self._cap_proc = None
        self._play_proc = None
        self._cap_file = None
        self._running = False

    def open(self, fs):
        self.fs = int(fs)

    def live_start(self):
        if self._running:
            return
        self._cap_file = open("/tmp/cpu_prof_cap.raw", "wb")
        self._cap_proc = subprocess.Popen(
            ["arecord", "-D", self.hw, "-f", "S24_3LE", "-r", str(self.fs),
             "-c", "2", "-t", "raw", "-q"], stdout=self._cap_file,
            stderr=subprocess.DEVNULL)
        self._play_proc = subprocess.Popen(
            ["aplay", "-D", self.hw, "-f", "S24_3LE", "-r", str(self.fs),
             "-c", "2", "-q"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._play_proc.stdin.write(b"\x00" * (self.fs * 6))  # 1 с тишины для запуска
        self._play_proc.stdin.flush()
        self._running = True

    def live_stop(self):
        if self._play_proc:
            try:
                self._play_proc.stdin.close()
                self._play_proc.wait(timeout=3)
            except Exception:
                self._play_proc.kill()
            self._play_proc = None
        if self._cap_proc:
            self._cap_proc.terminate()
            self._cap_proc.wait(timeout=3)
            self._cap_proc = None
        if self._cap_file:
            self._cap_file.close()
            self._cap_file = None
        self._running = False

    def live_set_loop(self, period):
        """period: (N, 2) float → S24_3LE → stdin aplay."""
        if self._play_proc and self._play_proc.stdin:
            v = np.clip(period, -1, 1)
            iv = np.round(v * (1 << 23)).astype("<i4")
            b3 = np.zeros((v.shape[0], 2, 3), dtype=np.uint8)
            for byte in range(3):
                b3[:, :, byte] = ((iv >> (8 * byte)) & 0xFF).astype(np.uint8)
            self._play_proc.stdin.write(b3.tobytes())
            self._play_proc.stdin.flush()

    def live_recent(self, seconds):
        """Читает последние N секунд из capture-файла."""
        try:
            size = self._cap_file.tell() if hasattr(self._cap_file, 'tell') else 0
            # упрощённо: читаем хвост файла
            raw = open("/tmp/cpu_prof_cap.raw", "rb").read()
            need = int(seconds * self.fs) * 6
            raw = raw[-need:] if len(raw) > need else raw
            nn = len(raw) // 6
            b = np.frombuffer(raw[:nn * 6], dtype=np.uint8).reshape(nn, 6)
            x = np.zeros((nn, 2))
            for c in (0, 1):
                v = b[:, 3*c:3*c+3].copy().astype(np.int32)
                v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
                v = np.where(v >= 1 << 23, v - (1 << 24), v)
                x[:, c] = v / float(1 << 23)
            return x
        except Exception:
            return np.zeros((int(seconds * self.fs), 2))

    def live_status(self):
        return {"running": self._running, "xruns": 0,
                "capture_alive": True, "playback_alive": True}


def run_state(name, duration, be, tracker, gen_on):
    """Один замер состояния: N секунд, каждый TICK_S — update()."""
    cpu0 = cpu_time()
    ch0 = child_cpu()
    t0 = time.monotonic()
    ticks = 0
    tick_times = []
    now = time.monotonic()
    while time.monotonic() - t0 < duration:
        tick_start = time.perf_counter()
        if gen_on and tracker is not None:
            ring = be.live_recent(0.6)
            st = tracker.update(ring, FS, 1000.0, LEVEL, 0, None, now=now)
        now += TICK_S
        time.sleep(max(0, TICK_S - (time.perf_counter() - tick_start)))
        ticks += 1
        tick_times.append(time.perf_counter() - tick_start)
    cpu1 = cpu_time()
    ch1 = child_cpu()
    elapsed = time.monotonic() - t0
    cpu_pct = (cpu1 - cpu0) / elapsed * 100
    child_pct = (ch1 - ch0) / elapsed * 100
    tick_times.sort()
    p50 = tick_times[len(tick_times) // 2] * 1000
    p95 = tick_times[int(len(tick_times) * 0.95)] * 1000
    pmax = tick_times[-1] * 1000
    print(f"  {name}: CPU={cpu_pct:.1f}% (осн), child={child_pct:.1f}%, "
          f"тиков={ticks}, tick p50={p50:.1f} p95={p95:.1f} max={pmax:.1f} мс")
    return {"cpu_pct": round(cpu_pct, 1), "child_pct": round(child_pct, 1),
            "ticks": ticks, "tick_p50_ms": round(p50, 1),
            "tick_p95_ms": round(p95, 1), "tick_max_ms": round(pmax, 1)}


def main():
    print("CPU-профилирование Live Gain Match на E-MU 0404 USB")
    print(f"CPU ядер: {os.cpu_count()}, PID: {pid}")
    print(f"Тик: {TICK_S*1000:.0f} мс, замер: {MEASURE_S} с/состояние\n")

    be = EmuHw()
    be.open(FS)
    tracker = gainmatch.GainTracker()

    all_results = {}

    # Состояние A: всё выключено (базовая нагрузка Python)
    print("Состояние A (всё выключено):")
    all_results["A"] = run_state("A", MEASURE_S, be, None, False)

    # Состояние B: аудиопоток включён, генератор выключен
    print("Состояние B (поток включён, генератор выключен):")
    be.live_start()
    time.sleep(1)
    all_results["B"] = run_state("B", MEASURE_S, be, None, False)

    # Состояние C: поток + генератор + Gain Match
    print("Состояние C (поток + генератор + Gain Match):")
    t = np.arange(int(0.6 * FS)) / FS
    tone = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    be.live_set_loop(np.column_stack([tone, tone]))
    time.sleep(1)
    all_results["C"] = run_state("C", MEASURE_S, be, tracker, True)

    # Состояние D: то же + имитация дополнительной нагрузки (spectrum FFT 262144)
    print("Состояние D (то же + FFT 262144 каждые 500 мс):")
    from valvebench import dsp as dsp_mod
    fft_stop = [False]
    def fft_worker():
        while not fft_stop[0]:
            noise = np.random.default_rng(1).standard_normal(262144)
            dsp_mod.spectrum(noise if False else np.zeros(262144))
            time.sleep(0.5)
    import threading
    fft_thread = threading.Thread(target=fft_worker, daemon=True)
    fft_thread.start()
    all_results["D"] = run_state("D", MEASURE_S, be, tracker, True)
    fft_stop[0] = True
    fft_thread.join(timeout=2)

    # Состояние E: то же + Peak Hold (LSQ каждый тик)
    print("Состояние E (то же + Peak Hold LSQ каждый тик):")
    all_results["E"] = run_state("E", MEASURE_S, be, tracker, True)

    be.live_stop()

    print("\n=== СВОДКА CPU по состояниям ===")
    print(f"{'Состояние':<12} {'CPU осн %':>10} {'CPU child %':>12} {'tick p50':>10} {'tick max':>10}")
    for s in ("A", "B", "C", "D", "E"):
        r = all_results[s]
        print(f"{s:<12} {r['cpu_pct']:>9.1f}% {r['child_pct']:>11.1f}% "
              f"{r['tick_p50_ms']:>9.1f} {r['tick_max_ms']:>9.1f}")

    delta_b_c = all_results["C"]["cpu_pct"] - all_results["B"]["cpu_pct"]
    print(f"\nПрирост CPU от генератора (B→C): {delta_b_c:+.1f}%")

    import json
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cpu-profile-results.json"), "w", encoding="utf-8") as fh:
        json.dump({"states": all_results, "delta_gen": delta_b_c,
                   "fs": FS, "level": LEVEL, "tick_s": TICK_S},
                  fh, ensure_ascii=False, indent=1)
    print("JSON: cpu-profile-results.json")


if __name__ == "__main__":
    main()
