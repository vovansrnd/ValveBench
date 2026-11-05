"""Ввод-вывод звука.

Почему aplay/arecord, а не PortAudio-дуплекс
--------------------------------------------
Захват и воспроизведение — два независимых ALSA-потока (два процесса), без
snd_pcm_link. USB-карты с implicit feedback (E-MU, многие UAC2) в связанном
дуплексе ведут себя нестабильно, а раздельные потоки проверены годами.
Расхождение стартов не важно: измерения построены так, что им не нужна
привязка по времени (когерентные тоны, свип с деконволюцией).

Здесь же — SimBackend: виртуальная карта с виртуальным усилителем для
отладки без железа и для тестов.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import tempfile
import math
from dataclasses import dataclass

import numpy as np

from . import dsp
from .pcm import BYTES, FORMATS, from_pcm, to_pcm


# ------------------------------------------------------------ обнаружение карт


def list_cards():
    """[(номер, «[id] имя»)] из /proc/asound/cards."""
    out = []
    try:
        txt = open("/proc/asound/cards").read()
    except OSError:
        return out
    for m in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)$", txt, re.M):
        out.append((int(m.group(1)), f"{m.group(2)} — {m.group(3).strip()}"))
    return out


def _pcm_hw_devices(tool):
    """Множество (card, device), реально перечисленных aplay/arecord -l."""
    try:
        p = subprocess.run([tool, "-l"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=3.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    found = set()
    for line in p.stdout.splitlines():
        m = re.match(r"^\s*card\s+(\d+):.*?,\s*device\s+(\d+):", line)
        if m:
            found.add((int(m.group(1)), int(m.group(2))))
    return found


def list_duplex_devices():
    """Список ALSA hw:CARD,DEV, присутствующих и в playback, и в capture.

    HDMI-only выходы исключаются: измерительные приборы требуют одновременно
    ЦАП и АЦП. Устройства выбираются по пересечению aplay -l и arecord -l,
    а не по одному лишь номеру звуковой карты.
    """
    playback = _pcm_hw_devices("aplay")
    capture = _pcm_hw_devices("arecord")
    names = dict(list_cards())
    result = []
    for card, device in sorted(playback & capture):
        label = names.get(card, f"ALSA card {card}")
        hw = f"hw:{card},{device}"
        result.append((hw, f"{hw} — {label}, device {device}"))
    return result


@dataclass
class AltSet:
    fmt: str
    channels: int
    rates: tuple


def parse_stream(card):
    """Разбор /proc/asound/cardN/stream0: {'Playback': [AltSet], 'Capture': [AltSet]}."""
    res = {"Playback": [], "Capture": []}
    try:
        lines = open(f"/proc/asound/card{card}/stream0").read().splitlines()
    except OSError:
        return res
    sec, cur = None, None

    def flush():
        if sec and cur and cur.get("fmt") and cur.get("ch") and cur.get("rates"):
            res[sec].append(AltSet(cur["fmt"], cur["ch"], tuple(cur["rates"])))

    for ln in lines:
        t = ln.strip()
        if t in ("Playback:", "Capture:"):
            flush()
            sec, cur = t[:-1], {}
        elif t.startswith("Altset") and sec:
            flush()
            cur = {}
        elif cur is not None and t.startswith("Format:"):
            cur["fmt"] = t.split(":", 1)[1].strip()
        elif cur is not None and t.startswith("Channels:"):
            cur["ch"] = int(t.split(":", 1)[1])
        elif cur is not None and t.startswith("Rates:"):
            cur["rates"] = [int(r) for r in re.findall(r"\d+", t.split(":", 1)[1])]
            if "-" in t and len(cur["rates"]) == 2:       # непрерывный диапазон
                cur["rates"] = [44100, 48000, 88200, 96000, 176400, 192000]
    flush()
    return res


def choose_format(alts, fs):
    """(fmt, channels) для частоты fs: предпочтительный формат, минимум каналов ≥2."""
    ok = [a for a in alts if fs in a.rates and a.channels >= 2]
    if not ok:
        return None
    ok.sort(key=lambda a: (FORMATS.index(a.fmt) if a.fmt in FORMATS else 99, a.channels))
    return ok[0].fmt, ok[0].channels


# ------------------------------------------------------------------ интерфейс


class BackendError(RuntimeError):
    pass


class Backend:
    """Общий интерфейс. Все функции блокирующие; зови из рабочего потока."""

    name = "?"
    fs = 48000

    def open(self, fs):                    # noqa: D401
        raise NotImplementedError

    def play_record(self, out, tail=0.8):
        """out: (n,) или (n,2) float → запись (m,2) float64, m ≈ n + tail·fs."""
        raise NotImplementedError

    # живой режим
    def live_start(self):
        raise NotImplementedError

    def live_stop(self):
        raise NotImplementedError

    def live_set_loop(self, period):       # period: (n,2) float32 или None (тишина)
        raise NotImplementedError

    def live_recent(self, seconds):
        raise NotImplementedError

    def live_status(self):
        return {"backend": self.name, "running": bool(self.live_running), "xruns": None,
                "capture_frames": 0, "playback_frames": 0, "capture_alive": bool(self.live_running),
                "playback_alive": bool(self.live_running), "details": "backend does not expose ALSA xrun diagnostics"}

    live_running = False
    notes: list


# --------------------------------------------------------------------- ALSA


class AlsaCli(Backend):
    """hw:N,0 через процессы aplay и arecord со стандартным вводом-выводом.

    in_map / out_map — какие из аппаратных каналов считать L и R
    (у E-MU 0404 первая пара — аналог, далее S/PDIF)."""

    name = "alsa"

    def __init__(self, hw="hw:0,0", in_map=(0, 1), out_map=(0, 1), buffer_ms=100):
        self.hw = hw
        m = re.match(r"hw:(\d+)", hw)
        self.card = int(m.group(1)) if m else None
        self.in_map, self.out_map = tuple(in_map), tuple(out_map)
        self.buffer_ms = buffer_ms
        self.fmt_in = self.fmt_out = "S24_3LE"
        self.ch_in = self.ch_out = 2
        self.notes = []
        self._live_cap = None
        self._live_play = None
        self._live_stop = threading.Event()
        self._ring_capacity = int(12.0 * self.fs)
        self._ring = np.zeros((self._ring_capacity, 2), dtype=np.float64)
        self._ring_pos = 0
        self._ring_count = 0
        self._ring_lock = threading.Lock()
        self._loop = None
        self._loop_lock = threading.Lock()
        self._threads = []
        self._live_err_files = []
        self.live_error = ""
        self.live_running = False
        self._live_capture_frames = 0
        self._live_playback_frames = 0
        self._live_xruns_capture = 0
        self._live_xruns_playback = 0
        self._live_log_offsets = [0, 0]
        self._live_started_at = None
        self._live_last_status = None

    # -- настройка

    def open(self, fs):
        self.fs = int(fs)
        self.notes = []
        if self.card is not None:
            st = parse_stream(self.card)
            ci, co = choose_format(st["Capture"], self.fs), choose_format(st["Playback"], self.fs)
            if ci is None or co is None:
                self.notes.append(f"карта не заявляет {self.fs} Гц с ≥2 каналами "
                                  f"(/proc/asound/card{self.card}/stream0) — пробую 2 канала, S24_3LE")
            else:
                (self.fmt_in, self.ch_in), (self.fmt_out, self.ch_out) = ci, co
        if max(self.in_map) >= self.ch_in or max(self.out_map) >= self.ch_out:
            raise BackendError(f"карта открывается как {self.ch_in} вх / {self.ch_out} вых, "
                               f"а выбраны каналы {self.in_map}/{self.out_map}")

    def _cmd(self, tool, direction, quiet=True):
        fmt, ch = (self.fmt_in, self.ch_in) if direction == "c" else (self.fmt_out, self.ch_out)
        c = [tool]
        if quiet:
            c.append("-q")
        c += ["-D", self.hw, "-t", "raw", "-f", fmt, "-r", str(self.fs), "-c", str(ch)]
        if direction == "p":
            c += [f"--buffer-time={self.buffer_ms * 1000}"]
        return c

    def _to_hw_out(self, out):
        out = np.asarray(out, dtype=np.float64)
        if out.ndim == 1:
            out = np.stack([out, out], axis=1)
        hw = np.zeros((out.shape[0], self.ch_out))
        hw[:, self.out_map[0]] = out[:, 0]
        hw[:, self.out_map[1]] = out[:, 1]
        return hw

    def _from_hw_in(self, raw):
        v = from_pcm(raw, self.fmt_in, self.ch_in)
        return v[:, list(self.in_map)]

    # -- одиночный проход «играть + писать»

    def play_record(self, out, tail=0.8):
        """Один измерительный проход с конечными таймаутами.

        Захват запускается первым и пишет во временный файл, воспроизведение
        выполняется отдельным процессом. Это устраняет вечное чтение stdout и
        позволяет гарантированно завершить обе команды при ошибке ALSA.
        """
        if self.live_running:
            raise BackendError("живой режим занимает карту: сначала live_stop()")
        out = self._to_hw_out(out)
        n_out = out.shape[0]
        pad_n = int(0.05 * self.fs)
        pad = np.zeros((pad_n, self.ch_out), dtype=np.float64)
        data = to_pcm(np.vstack([pad, out, pad]), self.fmt_out)
        duration = (len(out) + 2 * pad_n) / self.fs + max(0.0, float(tail)) + 0.45
        record_seconds = max(1, int(math.ceil(duration)) + 1)
        rec_cmd = self._cmd("arecord", "c") + ["-d", str(record_seconds)]
        play_cmd = self._cmd("aplay", "p")
        timeout = max(12.0, duration + 8.0)
        notes = []

        with tempfile.TemporaryFile(mode="w+b") as capture:
            cap = subprocess.Popen(rec_cmd, stdout=capture, stderr=subprocess.PIPE)
            try:
                # Даём ALSA открыть PCM и начать запись до подачи сигнала.
                time.sleep(0.30)
                if cap.poll() is not None:
                    err = cap.stderr.read().decode("utf-8", "replace") if cap.stderr else ""
                    raise BackendError(f"arecord не смог открыть захват (код {cap.returncode}): {err.strip()}")
                try:
                    play = subprocess.run(play_cmd, input=data, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, timeout=timeout, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise BackendError(f"aplay завис дольше {timeout:.1f} с; процесс остановлен") from exc
                play_err = play.stderr.decode("utf-8", "replace")
                if play.returncode != 0:
                    raise BackendError(f"aplay завершился с кодом {play.returncode}: {play_err.strip()}")
                try:
                    cap.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    raise BackendError(f"arecord не завершился за {timeout:.1f} с; процесс остановлен") from exc
                cap_err = cap.stderr.read().decode("utf-8", "replace") if cap.stderr else ""
                if cap.returncode != 0:
                    raise BackendError(f"arecord завершился с кодом {cap.returncode}: {cap_err.strip()}")
                if re.search(r"underrun|overrun|xrun", cap_err, re.I):
                    notes.append(f"arecord: {cap_err.strip()}")
                if re.search(r"underrun|overrun|xrun", play_err, re.I):
                    notes.append(f"aplay: {play_err.strip()}")
                capture.seek(0)
                raw = capture.read()
            finally:
                if cap.poll() is None:
                    cap.terminate()
                    try:
                        cap.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        cap.kill()
                        cap.wait(timeout=1.5)

        frame = self.ch_in * BYTES[self.fmt_in]
        raw = raw[: len(raw) // frame * frame]
        min_frames = n_out + int(max(0.0, tail) * self.fs)
        if len(raw) < min_frames * frame * 0.75:
            raise BackendError(f"захват короче ожидаемого ({len(raw) // frame} кадров; нужно около {min_frames})")
        self.notes = notes
        return self._from_hw_in(raw)

    def record(self, seconds=5.0):
        """Запись внешнего сигнала с входов карты без воспроизведения."""
        if self.live_running:
            raise BackendError("живой режим уже использует карту")
        seconds = float(seconds)
        if not 0.1 <= seconds <= 600:
            raise BackendError("длительность записи должна быть от 0.1 до 600 секунд")
        cmd = self._cmd("arecord", "c") + ["-d", str(max(1, int(math.ceil(seconds))))]
        timeout = seconds + 8.0
        with tempfile.TemporaryFile(mode="w+b") as capture:
            try:
                proc = subprocess.run(cmd, stdout=capture, stderr=subprocess.PIPE,
                                      timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"arecord завис дольше {timeout:.1f} с; запись отменена") from exc
            err = proc.stderr.decode("utf-8", "replace")
            if proc.returncode != 0:
                raise BackendError(f"arecord завершился с кодом {proc.returncode}: {err.strip()}")
            capture.seek(0)
            raw = capture.read()
        frame = self.ch_in * BYTES[self.fmt_in]
        raw = raw[:len(raw) // frame * frame]
        x = self._from_hw_in(raw)
        self.notes = [err.strip()] if re.search(r"overrun|xrun", err, re.I) else []
        return x[:int(seconds * self.fs)]

    @staticmethod
    def _stderr(p):
        try:
            return p.stderr.read().decode("utf-8", "replace") if p.stderr else ""
        except Exception:
            return ""

    # -- живой режим

    def live_start(self):
        if self.live_running:
            return
        self._live_stop.clear()
        self._ring_capacity = int(12.0 * self.fs)
        with self._ring_lock:
            self._ring = np.zeros((self._ring_capacity, 2), dtype=np.float64)
            self._ring_pos = 0
            self._ring_count = 0
        self.live_error = ""
        self._live_capture_frames = 0
        self._live_playback_frames = 0
        self._live_xruns_capture = 0
        self._live_xruns_playback = 0
        self._live_log_offsets = [0, 0]
        self._live_started_at = time.monotonic()
        self._live_last_status = None
        try:
            # Не оставляем stderr=PIPE без читателя: поток xrun-предупреждений
            # способен заполнить pipe и намертво заблокировать процесс ALSA.
            self._live_err_files = [tempfile.TemporaryFile(mode="w+b"),
                                    tempfile.TemporaryFile(mode="w+b")]
            self._live_cap = subprocess.Popen(self._cmd("arecord", "c", quiet=False), stdout=subprocess.PIPE,
                                              stderr=self._live_err_files[0])
            self._live_play = subprocess.Popen(self._cmd("aplay", "p", quiet=False), stdin=subprocess.PIPE,
                                               stderr=self._live_err_files[1])
            time.sleep(0.20)
            failed = []
            for proc, name, err_file in ((self._live_cap, "arecord", self._live_err_files[0]),
                                         (self._live_play, "aplay", self._live_err_files[1])):
                if proc.poll() is not None:
                    err_file.seek(0)
                    err = err_file.read().decode("utf-8", "replace")
                    failed.append(f"{name} (код {proc.returncode}): {err.strip()}")
            if failed:
                raise BackendError("не удалось открыть live audio: " + "; ".join(failed))
            self.live_running = True
            self._threads = [threading.Thread(target=self._cap_loop, daemon=True),
                             threading.Thread(target=self._play_loop, daemon=True)]
            for t in self._threads:
                t.start()
        except Exception:
            self.live_stop()  # единая процедура гарантированного завершения процессов
            raise

    def _cap_loop(self):
        frame = self.ch_in * BYTES[self.fmt_in]
        rest = b""
        while not self._live_stop.is_set():
            b = self._live_cap.stdout.read(frame * 2048)
            if not b:
                if not self._live_stop.is_set():
                    self._live_failure(f"arecord завершился неожиданно (код {self._live_cap.poll()})")
                break
            rest += b
            use = len(rest) // frame * frame
            if not use:
                continue
            blk = self._from_hw_in(rest[:use])
            rest = rest[use:]
            n = len(blk)
            if n == 0:
                continue
            self._live_capture_frames += n
            with self._ring_lock:
                cap = self._ring_capacity
                if n >= cap:
                    self._ring[:] = blk[-cap:]
                    self._ring_pos = 0
                    self._ring_count = cap
                else:
                    pos = self._ring_pos
                    first = min(n, cap - pos)
                    self._ring[pos:pos + first] = blk[:first]
                    remaining = n - first
                    if remaining:
                        self._ring[:remaining] = blk[first:]
                    self._ring_pos = (pos + n) % cap
                    self._ring_count = min(cap, self._ring_count + n)

    def _play_loop(self):
        silence = np.zeros((self.fs // 10, 2))
        pos = 0
        while not self._live_stop.is_set():
            with self._loop_lock:
                loop = self._loop
            if loop is None:
                buf, pos = silence, 0
            else:
                n = len(loop)
                step = min(n, self.fs // 20)
                idx = (pos + np.arange(step)) % n
                buf, pos = loop[idx], (pos + step) % n
            try:
                self._live_play.stdin.write(to_pcm(self._to_hw_out(buf), self.fmt_out))
                self._live_play.stdin.flush()
                self._live_playback_frames += len(buf)
            except (BrokenPipeError, ValueError, OSError) as exc:
                if not self._live_stop.is_set():
                    self._live_failure(f"aplay прекратил воспроизведение: {type(exc).__name__}: {exc}")
                break

    def _live_failure(self, message):
        self.live_error = message
        self.live_running = False
        self._live_stop.set()

    def live_set_loop(self, period):
        if period is None:
            value = None
        else:
            value = np.asarray(period, dtype=np.float64)
            if value.ndim != 2 or value.shape[1] != 2 or value.shape[0] == 0:
                raise BackendError("live loop должен иметь форму (N, 2), N > 0")
            if not np.all(np.isfinite(value)):
                raise BackendError("live loop содержит NaN или бесконечность")
        with self._loop_lock:
            self._loop = value

    def live_recent(self, seconds):
        wanted = max(0, int(float(seconds) * self.fs))
        with self._ring_lock:
            n = min(wanted, self._ring_count)
            if n <= 0:
                return np.zeros((0, 2), dtype=np.float64)
            start = (self._ring_pos - n) % self._ring_capacity
            end = start + n
            if end <= self._ring_capacity:
                return self._ring[start:end].copy()
            return np.vstack((self._ring[start:], self._ring[:end - self._ring_capacity])).copy()

    def live_status(self):
        """Snapshot counters and ALSA xrun messages without moving shared file offsets."""
        import os
        import re

        for idx, err_file in enumerate(self._live_err_files):
            try:
                size = os.fstat(err_file.fileno()).st_size
                offset = self._live_log_offsets[idx]
                if size > offset:
                    chunk = os.pread(err_file.fileno(), size - offset, offset).decode("utf-8", "replace")
                    self._live_log_offsets[idx] = size
                    hits = len(re.findall(r"underrun|overrun|xrun", chunk, re.I))
                    if idx == 0:
                        self._live_xruns_capture += hits
                    else:
                        self._live_xruns_playback += hits
            except (OSError, ValueError):
                pass
        cap_alive = self._live_cap is not None and self._live_cap.poll() is None
        play_alive = self._live_play is not None and self._live_play.poll() is None
        status = {
            "backend": "alsa", "running": bool(self.live_running),
            "capture_alive": cap_alive, "playback_alive": play_alive,
            "capture_frames": int(self._live_capture_frames),
            "playback_frames": int(self._live_playback_frames),
            "capture_seconds": self._live_capture_frames / max(1, self.fs),
            "playback_seconds": self._live_playback_frames / max(1, self.fs),
            "capture_xruns": int(self._live_xruns_capture),
            "playback_xruns": int(self._live_xruns_playback),
            "xruns": int(self._live_xruns_capture + self._live_xruns_playback),
            "details": self.live_error or "ALSA stderr checked for underrun/overrun messages",
        }
        self._live_last_status = status
        return status

    def live_stop(self):
        # Идемпотентно: вызывается и после аварийного выхода одного из потоков.
        self._live_stop.set()
        procs = (self._live_play, self._live_cap)
        for proc in procs:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        for proc in procs:
            if proc is None:
                continue
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.live_error = (self.live_error + "; " if self.live_error else "") + "процесс ALSA не завершился после kill"
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
        for thread in self._threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._threads = []
        for err_file in self._live_err_files:
            try:
                err_file.close()
            except Exception:
                pass
        self._live_err_files = []
        self._live_play = self._live_cap = None
        self.live_running = False


# ------------------------------------------------------------ виртуальная карта


class SimChannel:
    """Виртуальный «усилитель»: нелинейность → RIAA → усиление → шум/фон."""

    def __init__(self, gain=1.0, a2=0.0, a3=0.0, riaa=False, iec=False,
                 noise_dbfs=-110.0, hum50_dbfs=-120.0, hum100_dbfs=-125.0, clip=1.0,
                 quant_bits=None):
        # quant_bits — разрядность «преобразователя»: квантование точно периодического
        # сигнала даёт линии на гармониках тона с уровнем, не зависящим от амплитуды.
        self.quant_bits = quant_bits
        self.gain, self.a2, self.a3, self.riaa, self.iec = gain, a2, a3, riaa, iec
        self.noise, self.h50, self.h100, self.clip = noise_dbfs, hum50_dbfs, hum100_dbfs, clip


class SimBackend(Backend):
    """Карта-пустышка с задержкой, шумом и настраиваемым DUT на каждом канале."""

    name = "sim"

    def __init__(self, left=None, right=None, delay_ms=137.0, seed=1):
        self.ch = [left or SimChannel(), right or SimChannel()]
        self.delay = int(delay_ms * 1e-3 * 48000)
        self.rng = np.random.default_rng(seed)
        self.notes = []
        self._loop = None
        self.live_running = False
        self._t0 = 0.0
        self.crosstalk_db = -90.0

    def open(self, fs):
        self.fs = int(fs)

    def _dut(self, x, c, tt):
        from scipy.signal import resample_poly
        xu = resample_poly(x, 4, 1)
        yu = xu + c.a2 * xu ** 2 + c.a3 * xu ** 3
        y = resample_poly(yu, 1, 4)[: len(x)]
        if c.riaa:
            nf = 1 << int(np.ceil(np.log2(len(y) * 2)))
            Y = np.fft.rfft(y, nf) * dsp.riaa_playback(np.fft.rfftfreq(nf, 1 / self.fs), c.iec)
            y = np.fft.irfft(Y, nf)[: len(x)]
        y = y * c.gain
        if c.quant_bits:
            q = 2.0 ** (int(c.quant_bits) - 1)
            y = np.round(y * q) / q
        # noise_dbfs — суммарный шум в полосе 20 Гц–20 кГц, уровень как у синуса
        sigma = dsp.dbfs_to_amp(c.noise) / np.sqrt(2) * np.sqrt(0.5 * self.fs / 20000.0)
        y = y + sigma * self.rng.standard_normal(len(y))
        y = y + dsp.dbfs_to_amp(c.h50) * np.sin(2 * np.pi * 50 * tt) + dsp.dbfs_to_amp(c.h100) * np.sin(2 * np.pi * 100 * tt + 1)
        return np.clip(y, -c.clip, c.clip)

    def play_record(self, out, tail=0.8):
        out = np.asarray(out, dtype=np.float64)
        if out.ndim == 1:
            out = np.stack([out, out], axis=1)
        d = int(self.delay * self.fs / 48000)
        n = len(out) + d + int(tail * self.fs) + int(0.3 * self.fs)
        tt = np.arange(n) / self.fs
        res = np.zeros((n, 2))
        padded = np.vstack([np.zeros((d, 2)), out, np.zeros((n - d - len(out), 2))])
        for i in (0, 1):
            x = padded[:, i] + dsp.dbfs_to_amp(self.crosstalk_db) * padded[:, 1 - i]
            res[:, i] = self._dut(x, self.ch[i], tt)
        return res

    def record(self, seconds=5.0):
        n = max(1, int(float(seconds) * self.fs))
        # Симулятор не моделирует проигрыватель: возвращает тишину с малым шумом.
        return self.rng.normal(0.0, 1e-6, (n, 2))

    # живой режим: данные «по запросу»
    def live_start(self):
        self.live_running = True
        self._t0 = time.monotonic()

    def live_stop(self):
        self.live_running = False

    def live_status(self):
        return {"backend": "sim", "running": bool(self.live_running), "capture_alive": bool(self.live_running),
                "playback_alive": bool(self.live_running), "capture_frames": 0, "playback_frames": 0,
                "xruns": 0, "capture_xruns": 0, "playback_xruns": 0,
                "details": "simulator: hardware xruns do not apply"}

    def live_set_loop(self, period):
        if period is None:
            self._loop = None
            return
        value = np.asarray(period, dtype=np.float64)
        if value.ndim != 2 or value.shape[1] != 2 or value.shape[0] == 0:
            raise BackendError("live loop должен иметь форму (N, 2), N > 0")
        if not np.all(np.isfinite(value)):
            raise BackendError("live loop содержит NaN или бесконечность")
        self._loop = value

    def live_recent(self, seconds):
        n = int(seconds * self.fs)
        if self._loop is None:
            x = np.zeros((n, 2))
        else:
            reps = int(np.ceil(n / len(self._loop)))
            x = np.tile(self._loop, (reps, 1))[:n]
        d = int(self.delay * self.fs / 48000)
        return self.play_record(x, tail=0.0)[d:d + n] if n else np.zeros((0, 2))
