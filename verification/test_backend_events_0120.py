"""События потока без звуковой карты: гонка опроса логов, интервалы XRUN, ложные «паузы» (0.1.20-dev2)."""
import os, sys, tempfile, threading, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from valvebench.backend import AlsaCli, xrun_suspect

ok = 0
def check(name, cond, info=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + name + (f": {info}" if info else ""))
    if not cond: raise SystemExit(1)
    ok += 1

# 1. гонка: 8 потоков опрашивают один и тот же файл
be = AlsaCli("hw:99,0"); be.fs = 48000
f = tempfile.TemporaryFile(mode="w+b")
be._live_err_files = [f, tempfile.TemporaryFile(mode="w+b")]
be._live_log_offsets = [0, 0]; be._ring_total = 1000
N = 3000
f.write(b"".join(b"overrun!!! (at least %d.500 ms long)\n" % (i % 90) for i in range(N))); f.flush()
stop = threading.Event()
def hammer():
    while not stop.is_set():
        be._poll_logs()
ths = [threading.Thread(target=hammer) for _ in range(8)]
for t in ths: t.start()
time.sleep(0.8); stop.set()
for t in ths: t.join()
check("8 потоков опроса: каждое XRUN-сообщение зарегистрировано ровно один раз",
      be._live_xruns_capture == N and len(be._events) == min(N, 512), f"{be._live_xruns_capture} из {N}")

# 2. интервалы с учётом длительности
fs = 192000; tail = 10 * fs
a, b = xrun_suspect("capture", tail, fs, 300.0)
check("capture overrun 300 мс: интервал [−0.15 с; +0.35 с]", a == tail - int(0.15 * fs) and b == tail + int(0.35 * fs), f"{(a-tail)/fs:+.2f}…{(b-tail)/fs:+.2f} с")
a, b = xrun_suspect("playback", tail, fs, 2000.0)
check("playback underrun 2 с: интервал покрывает [−2.1 с; +3.2 с]", a == tail - int(2.1 * fs) and b == tail + int(3.2 * fs), f"{(a-tail)/fs:+.2f}…{(b-tail)/fs:+.2f} с")
a, b = xrun_suspect("capture", tail, fs, None)
check("неизвестная длительность трактуется как 0.1 с", b == tail + int(0.15 * fs))
print(f"\n{ok} проверок пройдено")
