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
import glob
import re
import subprocess
import threading
import time
import tempfile
import math
import collections
from dataclasses import dataclass

import numpy as np

from . import dsp
from .pcm import BYTES, FORMATS, from_pcm, to_pcm




# ----------------------------------------------------- события живого потока (0.1.20)

_XRUN_WORD = re.compile(r"underrun|overrun|xrun", re.I)
_XRUN_LEN = re.compile(r"at least\s+([\d.]+)\s*(ms|frames|samples)", re.I)


def parse_alsa_log(text):
    """Разобрать stderr aplay/arecord построчно: ([{'dur_ms','dur_frames','text'}], остаток).

    Последняя строка без перевода строки не разбирается, а возвращается как остаток:
    сообщение, разорванное границей чтения, раньше терялось (слово «over|run» пополам).
    Длительность берётся из «(at least N ms long)»; вариант в кадрах/сэмплах учитывается тоже.
    Это оценка СНИЗУ: реальный пропуск мог быть длиннее."""
    lines = text.split("\n")
    rest = lines.pop()
    events = []
    for ln in lines:
        if not _XRUN_WORD.search(ln):
            continue
        ev = {"dur_ms": None, "dur_frames": None, "text": ln.strip()[:200]}
        m = _XRUN_LEN.search(ln)
        if m:
            val, unit = float(m.group(1)), m.group(2).lower()
            if unit == "ms":
                ev["dur_ms"] = val
            else:
                ev["dur_frames"] = val
        events.append(ev)
    return events, rest


def xrun_suspect(kind, tail, fs, dur_ms=None):
    """Подозрительный интервал [a, b] в кадрах кольцевого буфера для события XRUN.

    capture (overrun АЦП): потерянные отсчёты в записи ОТСУТСТВУЮТ, а в позиции потери стоит
      разрыв; позиция известна с точностью опроса/очереди pipe (запас 0.15 с назад), после
      перезапуска потока возможен переходный процесс (0.05 с + длительность вперёд).
    playback (underrun ЦАП): тишина в сигнале длилась ≥ dur (началась за dur до сообщения) и
      дойдёт до захвата позже на задержку буферов воспроизведения (до ~0.6 с; берём 1.2 с)
      плюс длительность. Неизвестная длительность считается равной 0.1 с."""
    d = (float(dur_ms) / 1000.0) if dur_ms else 0.1
    if kind == "capture":
        return int(tail - 0.15 * fs), int(tail + (0.05 + d) * fs)
    return int(tail - (0.10 + d) * fs), int(tail + (1.20 + d) * fs)


def _set_pipe_size(fd, size):
    """Увеличить буфер pipe (Linux F_SETPIPE_SZ). Возвращает фактический размер или None."""
    try:
        import fcntl
        return int(fcntl.fcntl(fd, getattr(fcntl, "F_SETPIPE_SZ", 1031), int(size)))
    except Exception:  # noqa: BLE001 — не-Linux, лимит pipe-max-size и т.п.
        return None


def _pipe_pending_bytes(fd):
    try:
        import fcntl
        import struct
        import termios
        return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
    except Exception:  # noqa: BLE001
        return 0


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


def supported_pcm_rates(hw, direction):
    """Advertised 2-channel rates for one direction of an ALSA hw:C,D PCM.

    ALSA USB stream descriptors are card-scoped, not reliably PCM-device-scoped;
    this is a capability hint. Exact format/rate support is confirmed by opening
    the requested PCM before a measurement.
    """
    if direction not in ("Playback", "Capture"):
        raise ValueError("direction must be Playback or Capture")
    m = re.match(r"^hw:(\d+)(?:,(\d+))?$", str(hw))
    if not m:
        return ()
    streams = parse_stream(int(m.group(1)))
    rates = {rate for alt in streams[direction] if alt.channels >= 2 for rate in alt.rates}
    return tuple(sorted(rates))


def supported_duplex_rates(hw):
    """Known common rates for a full-duplex hw:CARD,DEV endpoint.

    ALSA's USB stream descriptor is card-scoped, so this is a conservative
    advertised capability list, not a substitute for opening the exact PCM.
    The measurement backend still reports an ALSA error if a particular
    endpoint does not accept one of these rates.
    """
    m = re.match(r"^hw:(\d+)(?:,(\d+))?$", str(hw))
    if not m:
        return ()
    streams = parse_stream(int(m.group(1)))
    playback = {rate for alt in streams["Playback"] if alt.channels >= 2 for rate in alt.rates}
    capture = {rate for alt in streams["Capture"] if alt.channels >= 2 for rate in alt.rates}
    return tuple(sorted(playback & capture))


def list_pcm_devices():
    """Return independently enumerated playback/capture ALSA PCM endpoints.

    Unlike list_duplex_devices(), this also reports DAC-only and ADC-only
    interfaces, which is important for the separate-device steady test.
    """
    names = dict(list_cards())
    result = {"Playback": [], "Capture": []}
    for direction, tool in (("Playback", "aplay"), ("Capture", "arecord")):
        for card, device in sorted(_pcm_hw_devices(tool)):
            hw = f"hw:{card},{device}"
            label = names.get(card, f"ALSA card {card}")
            result[direction].append((hw, f"{hw} — {label}, PCM {device}"))
    return result



def usb_profile_identity(vid, pid, serial="", physical_port="", matching_devices=1, pcm=0):
    """Stable USB calibration key that survives port changes when unambiguous."""
    vid, pid = str(vid).lower(), str(pid).lower()
    serial = str(serial or "").strip()
    if serial:
        identity = f"usb:{vid}:{pid}:serial={serial}"
    elif int(matching_devices) <= 1:
        identity = f"usb:{vid}:{pid}"
    else:
        identity = f"usb:{vid}:{pid}:port={physical_port}"
    return f"{identity}:pcm{int(pcm)}"

def stable_device_identity(hw):
    """Stable physical identity for an ALSA hw:C,D endpoint.

    Prefer USB serial. Without a serial, use VID:PID when that device is unique;
    use the physical USB port only to distinguish two identical connected cards.
    PCI devices use their sysfs address. Never use the volatile ALSA card number.
    """
    value = str(hw or "sim")
    if value == "sim":
        return "sim"
    match = re.fullmatch(r"hw:(\d+)(?:,(\d+))?", value)
    if not match:
        return value
    card, pcm = int(match.group(1)), int(match.group(2) or 0)
    base = f"/sys/class/sound/card{card}/device"
    try:
        current = os.path.realpath(base)
    except OSError:
        current = ""
    node = current
    while node and node != "/":
        try:
            names = set(os.listdir(node))
        except OSError:
            names = set()
        if {"idVendor", "idProduct"}.issubset(names):
            def read(name):
                try:
                    return open(os.path.join(node, name), encoding="utf-8").read().strip()
                except OSError:
                    return ""
            vid, pid = read("idVendor"), read("idProduct")
            serial = read("serial")
            physical = os.path.basename(node)
            matches = set()
            if not serial:
                # Most audio cards have a unique VID:PID. In that common case
                # keep calibration when moved to another USB socket. If two
                # identical serial-less devices are connected, retain port ID.
                for candidate in glob.glob("/sys/bus/usb/devices/*"):
                    try:
                        cvid = open(os.path.join(candidate, "idVendor"), encoding="utf-8").read().strip()
                        cpid = open(os.path.join(candidate, "idProduct"), encoding="utf-8").read().strip()
                        cserial = open(os.path.join(candidate, "serial"), encoding="utf-8").read().strip() if os.path.exists(os.path.join(candidate, "serial")) else ""
                    except OSError:
                        continue
                    if cvid == vid and cpid == pid and not cserial:
                        matches.add(os.path.realpath(candidate))
            return usb_profile_identity(vid, pid, serial, physical, max(1, len(matches)), pcm)
        if {"vendor", "device"}.issubset(names) and os.path.basename(node).startswith("0000:"):
            def read_pci(name):
                try:
                    return open(os.path.join(node, name), encoding="utf-8").read().strip()
                except OSError:
                    return ""
            return f"pci:{read_pci('vendor')}:{read_pci('device')}:{os.path.basename(node)}:pcm{pcm}"
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    try:
        card_id = open(f"/proc/asound/card{card}/id", encoding="utf-8").read().strip()
    except OSError:
        card_id = f"card{card}"
    return f"alsa:{card_id}:pcm{pcm}"


def list_duplex_devices():
    """Список всех ALSA PCM endpoints, которые реально есть и в playback, и capture.

    HDMI-only выходы исключаются. Каждая пара card/device отображается отдельно:
    например, многоканальная USB-карта может предоставить разные PCM endpoints
    для наушников, линейных выходов и дополнительных интерфейсов.
    """
    playback = _pcm_hw_devices("aplay")
    capture = _pcm_hw_devices("arecord")
    names = dict(list_cards())
    result = []
    for card, device in sorted(playback & capture):
        label = names.get(card, f"ALSA card {card}")
        hw = f"hw:{card},{device}"
        rates = supported_duplex_rates(hw)
        rate_text = ", ".join(f"{r / 1000:g}" for r in rates) if rates else "проверка частот при открытии"
        result.append((hw, f"{hw} — {label}, PCM {device} · {rate_text} кГц"))
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

    def live_recent_stamped(self, seconds):
        """(данные, номер_кадра_конца) — атомарная пара. Номер None, если backend не ведёт счёт."""
        return self.live_recent(seconds), None

    def live_events(self, since_frame=0):
        """События потока (XRUN, паузы прихода данных) с подозрительным интервалом в кадрах."""
        return []

    def live_status(self):
        return {"backend": self.name, "running": bool(self.live_running), "xruns": None,
                "capture_frames": 0, "playback_frames": 0, "capture_alive": bool(self.live_running),
                "playback_alive": bool(self.live_running), "capture_stale": False,
                "playback_stale": False, "capture_age_s": None, "playback_age_s": None,
                "details": "backend does not expose ALSA xrun diagnostics"}

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
        self._live_capture_last_progress_at = None
        self._live_playback_last_progress_at = None
        self._live_progress_timeout_s = 0.5
        self._live_last_status = None
        # 0.1.20: события потока с привязкой к позиции в кольце, большие буферы live-режима
        self.live_capture_buffer_ms = 400      # arecord --buffer-time (None = по умолчанию ALSA)
        self.live_playback_buffer_ms = 250     # aplay --buffer-time
        self.live_capture_pipe_bytes = 1 << 20
        self._ring_total = 0                   # кадров записано в кольцо (растёт под _ring_lock)
        self._events = collections.deque(maxlen=512)
        self._events_lock = threading.Lock()
        self._poll_lock = threading.Lock()     # весь цикл чтения/разбора stderr — один поток за раз
        self._live_carry = ["", ""]
        self._live_gap_max_s = 0.0
        self._live_buffers_used = None
        self._pipe_sizes = (None, None)

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

    def _cmd(self, tool, direction, quiet=True, buffer_ms=None):
        fmt, ch = (self.fmt_in, self.ch_in) if direction == "c" else (self.fmt_out, self.ch_out)
        c = [tool]
        if quiet:
            c.append("-q")
        c += ["-D", self.hw, "-t", "raw", "-f", fmt, "-r", str(self.fs), "-c", str(ch)]
        if direction == "p":
            c += [f"--buffer-time={int(buffer_ms if buffer_ms is not None else self.buffer_ms) * 1000}"]
        elif buffer_ms is not None:
            c += [f"--buffer-time={int(buffer_ms) * 1000}"]
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

        def _read_err(f):
            f.seek(0)
            return f.read().decode("utf-8", "replace")

        # stderr arecord — во временный файл, а не в pipe: при шторме XRUN (>64 КиБ сообщений)
        # он читался только после wait() и блокировал arecord до таймаута.
        with tempfile.TemporaryFile(mode="w+b") as capture, \
                tempfile.TemporaryFile(mode="w+b") as cap_err_file:
            cap = subprocess.Popen(rec_cmd, stdout=capture, stderr=cap_err_file)
            try:
                # Даём ALSA открыть PCM и начать запись до подачи сигнала.
                time.sleep(0.30)
                if cap.poll() is not None:
                    err = _read_err(cap_err_file)
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
                cap_err = _read_err(cap_err_file)
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

    def _open_live_processes(self, cap_buf, play_buf):
        """Запустить arecord/aplay; вернуть список ошибок (пусто — оба открылись)."""
        self._live_err_files = [tempfile.TemporaryFile(mode="w+b"),
                                tempfile.TemporaryFile(mode="w+b")]
        self._live_cap = subprocess.Popen(
            self._cmd("arecord", "c", quiet=False, buffer_ms=cap_buf),
            stdout=subprocess.PIPE, stderr=self._live_err_files[0])
        self._live_play = subprocess.Popen(
            self._cmd("aplay", "p", quiet=False, buffer_ms=play_buf),
            stdin=subprocess.PIPE, stderr=self._live_err_files[1])
        time.sleep(0.20)
        failed = []
        for proc, name, err_file in ((self._live_cap, "arecord", self._live_err_files[0]),
                                     (self._live_play, "aplay", self._live_err_files[1])):
            if proc.poll() is not None:
                err_file.seek(0)
                err = err_file.read().decode("utf-8", "replace")
                failed.append(f"{name} (код {proc.returncode}): {err.strip()}")
        return failed

    def live_start(self):
        if self.live_running:
            return
        self._live_stop.clear()
        self._ring_capacity = int(12.0 * self.fs)
        with self._ring_lock:
            self._ring = np.zeros((self._ring_capacity, 2), dtype=np.float64)
            self._ring_pos = 0
            self._ring_count = 0
            self._ring_total = 0
        self.live_error = ""
        self._live_capture_frames = 0
        self._live_playback_frames = 0
        self._live_xruns_capture = 0
        self._live_xruns_playback = 0
        self._live_log_offsets = [0, 0]
        self._live_carry = ["", ""]
        self._live_gap_max_s = 0.0
        with self._events_lock:
            self._events.clear()
        self._live_started_at = time.monotonic()
        self._live_capture_last_progress_at = None
        self._live_playback_last_progress_at = None
        self._live_last_status = None
        try:
            # stderr — во временные файлы (не pipe без читателя: поток xrun-сообщений способен
            # заполнить pipe и заблокировать ALSA-процесс); их читает монитор раз в 20 мс.
            # Большие буферы live-режима дают запас против пауз GUI/планировщика; если карта
            # их не принимает — повтор с прежними значениями.
            attempts = [(self.live_capture_buffer_ms, self.live_playback_buffer_ms),
                        (None, self.buffer_ms)]
            failed = []
            for cap_buf, play_buf in attempts:
                failed = self._open_live_processes(cap_buf, play_buf)
                if not failed:
                    self._live_buffers_used = (cap_buf, play_buf)
                    break
                self.live_stop()
                self._live_stop.clear()
                self._live_log_offsets = [0, 0]
            if failed:
                raise BackendError("не удалось открыть live audio: " + "; ".join(failed))
            frame_out = self.ch_out * BYTES[self.fmt_out]
            play_target = 1 << max(16, int(math.log2(max(1, 0.25 * self.fs * frame_out))))
            self._pipe_sizes = (
                _set_pipe_size(self._live_cap.stdout.fileno(), self.live_capture_pipe_bytes),
                _set_pipe_size(self._live_play.stdin.fileno(), min(play_target, 1 << 20)))
            self.live_running = True
            self._threads = [threading.Thread(target=self._cap_loop, daemon=True),
                             threading.Thread(target=self._play_loop, daemon=True),
                             threading.Thread(target=self._monitor_loop, daemon=True)]
            for t in self._threads:
                t.start()
        except Exception:
            self.live_stop()  # единая процедура гарантированного завершения процессов
            raise

    def _cap_loop(self):
        frame = self.ch_in * BYTES[self.fmt_in]
        rest = b""
        last_arrival = time.monotonic()
        period = None            # типичный интервал между «пачками» данных (период ALSA)
        while not self._live_stop.is_set():
            b = self._live_cap.stdout.read(frame * 2048)
            if not b:
                if not self._live_stop.is_set():
                    self._live_failure(f"arecord завершился неожиданно (код {self._live_cap.poll()})")
                break
            now = time.monotonic()
            rest += b
            use = len(rest) // frame * frame
            if not use:
                continue
            blk = self._from_hw_in(rest[:use])
            rest = rest[use:]
            n = len(blk)
            if n == 0:
                continue
            # Пауза прихода данных: либо хост не успевал читать (данные придут пачкой и без
            # потерь), либо устройство молчало. Само по себе потерей не считается — это
            # информационное событие; потерю подтверждают XRUN и детекторы в данных.
            # arecord отдаёт данные ПЕРИОДАМИ (с буфером 400 мс — раз в ~100 мс), поэтому
            # порог привязан к типичному интервалу между пачками, а не к интервалу между
            # чтениями по 2048 кадров (так ложно срабатывало на каждом периоде).
            gap = now - last_arrival
            last_arrival = now
            if gap >= 0.003:
                period = gap if period is None else 0.9 * period + 0.1 * min(gap, 2.5 * period)
                if self._ring_total > self.fs and gap > max(0.15, 3.0 * period):
                    self._live_gap_max_s = max(self._live_gap_max_s, gap)
                    self._add_event("capture_gap", self._ring_total, gap_ms=gap * 1e3,
                                    suspect=(self._ring_total - int(0.05 * self.fs),
                                             self._ring_total + int(0.05 * self.fs)),
                                    fatal=False)
            self._live_capture_frames += n
            self._live_capture_last_progress_at = now
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
                self._ring_total += n

    def _monitor_loop(self):
        while not self._live_stop.wait(0.02):
            try:
                self._poll_logs()
            except Exception:  # noqa: BLE001 — монитор не должен падать
                pass

    def _add_event(self, kind, frame, suspect, fatal, **extra):
        ev = {"kind": kind, "frame": int(frame), "t": time.monotonic(),
              "suspect": (int(suspect[0]), int(suspect[1])), "fatal": bool(fatal)}
        ev.update(extra)
        with self._events_lock:
            self._events.append(ev)

    def _poll_logs(self):
        """Прочитать новые строки stderr обоих процессов; каждое XRUN-сообщение — событие.

        Позиция события — конец потока в кадрах на момент разбора (с учётом данных, уже
        лежащих в pipe, но ещё не прочитанных). Подозрительный интервал выбран с запасом:
        для overrun АЦП сообщение приходит сразу после потери, для underrun ЦАП дефект
        дойдёт до захвата только через задержку буферов воспроизведения."""
        with self._poll_lock:
            self._poll_logs_locked()

    def _poll_logs_locked(self):
        # Вызывается и монитором (каждые 20 мс), и live_status() из GUI: без общей блокировки
        # два потока читали один и тот же диапазон stderr и регистрировали XRUN дважды.
        import os
        with self._events_lock:
            files = list(self._live_err_files)
        for idx, err_file in enumerate(files):
            try:
                size = os.fstat(err_file.fileno()).st_size
                off = self._live_log_offsets[idx]
                if size <= off:
                    continue
                chunk = os.pread(err_file.fileno(), size - off, off)
                self._live_log_offsets[idx] = size
            except (OSError, ValueError):
                continue
            text = self._live_carry[idx] + chunk.decode("utf-8", "replace")
            events, self._live_carry[idx] = parse_alsa_log(text)
            if not events:
                continue
            pending = 0
            try:
                if self._live_cap is not None and self._live_cap.stdout is not None:
                    pending = _pipe_pending_bytes(self._live_cap.stdout.fileno()) // max(
                        1, self.ch_in * BYTES[self.fmt_in])
            except (OSError, ValueError):
                pending = 0
            tail = self._ring_total + pending
            for ev in events:
                dur = ev["dur_ms"] if ev["dur_ms"] is not None else (
                    ev["dur_frames"] / self.fs * 1e3 if ev["dur_frames"] else None)
                if idx == 0:
                    self._live_xruns_capture += 1
                    self._add_event("xrun_capture", tail,
                                    suspect=xrun_suspect("capture", tail, self.fs, dur),
                                    fatal=True, dur_ms=dur, text=ev["text"])
                else:
                    self._live_xruns_playback += 1
                    self._add_event("xrun_playback", tail,
                                    suspect=xrun_suspect("playback", tail, self.fs, dur),
                                    fatal=True, dur_ms=dur, text=ev["text"])

    def live_events(self, since_frame=0):
        """События, подозрительный интервал которых заканчивается не раньше since_frame."""
        with self._events_lock:
            return [dict(e) for e in self._events if e["suspect"][1] >= int(since_frame)]

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
                self._live_playback_last_progress_at = time.monotonic()
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
        return self.live_recent_stamped(seconds)[0]

    def live_recent_stamped(self, seconds):
        """(данные, номер кадра конца окна). Пара снимается под одной блокировкой."""
        wanted = max(0, int(float(seconds) * self.fs))
        with self._ring_lock:
            total = self._ring_total
            n = min(wanted, self._ring_count)
            if n <= 0:
                return np.zeros((0, 2), dtype=np.float64), total
            start = (self._ring_pos - n) % self._ring_capacity
            end = start + n
            if end <= self._ring_capacity:
                return self._ring[start:end].copy(), total
            return np.vstack((self._ring[start:], self._ring[:end - self._ring_capacity])).copy(), total

    def live_status(self):
        """Снимок счётчиков и XRUN-сообщений ALSA (логи читает монитор; здесь — дочитывание)."""
        try:
            self._poll_logs()
        except Exception:  # noqa: BLE001
            pass
        cap_alive = self._live_cap is not None and self._live_cap.poll() is None
        play_alive = self._live_play is not None and self._live_play.poll() is None
        now = time.monotonic()
        cap_ref = self._live_capture_last_progress_at or self._live_started_at
        play_ref = self._live_playback_last_progress_at or self._live_started_at
        cap_age = max(0.0, now - cap_ref) if cap_ref is not None else None
        play_age = max(0.0, now - play_ref) if play_ref is not None else None
        timeout_s = float(getattr(self, "_live_progress_timeout_s", 0.5))
        running = bool(self.live_running)
        with self._events_lock:
            n_events = len(self._events)
            last = dict(self._events[-1]) if self._events else None
        status = {
            "backend": "alsa", "running": running,
            "capture_alive": cap_alive, "playback_alive": play_alive,
            "capture_age_s": cap_age, "playback_age_s": play_age,
            "capture_stale": bool(running and (cap_age is None or cap_age > timeout_s)),
            "playback_stale": bool(running and (play_age is None or play_age > timeout_s)),
            "capture_frames": int(self._live_capture_frames),
            "playback_frames": int(self._live_playback_frames),
            "capture_seconds": self._live_capture_frames / max(1, self.fs),
            "playback_seconds": self._live_playback_frames / max(1, self.fs),
            "capture_xruns": int(self._live_xruns_capture),
            "playback_xruns": int(self._live_xruns_playback),
            "xruns": int(self._live_xruns_capture + self._live_xruns_playback),
            "events": n_events, "last_event": last,
            "gap_max_ms": self._live_gap_max_s * 1e3,
            "buffers_ms": self._live_buffers_used, "pipe_bytes": self._pipe_sizes,
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
        with self._events_lock:
            files, self._live_err_files = self._live_err_files, []
        for err_file in files:
            try:
                err_file.close()
            except Exception:
                pass
        self._live_play = self._live_cap = None
        self.live_running = False


# ------------------------------------------------------------ виртуальная карта


class SimChannel:
    """Виртуальный «усилитель»: нелинейность → RIAA → усиление → шум/фон."""

    def __init__(self, gain=1.0, a2=0.0, a3=0.0, riaa=False, iec=False,
                 noise_dbfs=-110.0, hum50_dbfs=-120.0, hum100_dbfs=-125.0, clip=1.0,
                 quant_bits=None, mod_noise_dbc=None, mod_jitter_db=0.0, spurs=()):
        # mod_noise_dbc — шум, пропорциональный сигналу; jitter моделирует его разброс.
        # spurs — последовательность (частота, dBFS) синтетических стационарных линий.
        self.mod_noise_dbc = mod_noise_dbc
        self.mod_jitter_db = float(mod_jitter_db)
        self.spurs = tuple(spurs)
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
        if c.mod_noise_dbc is not None:
            g = 10 ** ((float(c.mod_noise_dbc) + self.rng.uniform(-c.mod_jitter_db, c.mod_jitter_db)) / 20.0)
            y = y * (1.0 + g * self.rng.standard_normal(len(y)))
        for hz, db in c.spurs:
            y = y + dsp.dbfs_to_amp(db) * np.sin(2 * np.pi * float(hz) * tt + 0.3)
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

    def live_events(self, since_frame=0):
        return []

    def live_recent_stamped(self, seconds):
        return self.live_recent(seconds), None

    def live_status(self):
        return {"backend": "sim", "running": bool(self.live_running), "capture_alive": bool(self.live_running),
                "playback_alive": bool(self.live_running), "capture_stale": False, "playback_stale": False,
                "capture_age_s": None, "playback_age_s": None,
                "capture_frames": 0, "playback_frames": 0,
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
