"""Текстовая диагностика для отчёта без скриншотов."""
from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, calib
from .selftest import run_selftest, format_selftest
from .backend import list_cards, list_duplex_devices, parse_stream


def _run(cmd, timeout=3.0):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout, check=False)
        return p.stdout.strip() or f"(код {p.returncode}, пустой вывод)"
    except FileNotFoundError:
        return "команда не найдена"
    except subprocess.TimeoutExpired:
        return f"таймаут {timeout:.1f} с — остановлено"
    except Exception as exc:  # noqa: BLE001
        return f"ошибка: {type(exc).__name__}: {exc}"


def _text_file(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "(нет)"


def make_report(include_selftest=True):
    lines = [
        f"ValveBench DEBUG REPORT v{__version__}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "=" * 72,
        "SYSTEM",
        f"OS: {platform.platform()}",
        f"Kernel: {_run(['uname', '-r'])}",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Architecture: {platform.machine()}",
        f"DISPLAY: {'set' if os.environ.get('DISPLAY') else 'not set'}; WAYLAND_DISPLAY: {'set' if os.environ.get('WAYLAND_DISPLAY') else 'not set'}",
        "",
        "PYTHON MODULES",
    ]
    for package in ("numpy", "scipy", "pycairo"):
        try:
            v = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            v = "not installed / not visible in this Python environment"
        lines.append(f"{package}: {v}")
    try:
        import gi
        lines.append(f"PyGObject module: import OK (gi {getattr(gi, '__version__', 'version not exposed')})")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        lines.append(f"GTK: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
        try:
            from . import ui  # noqa: F401
            lines.append("ValveBench GTK UI module: import OK")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"ValveBench GTK UI module: import failed ({type(exc).__name__}: {exc})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"GTK/PyGObject: unavailable ({type(exc).__name__}: {exc})")
    lines += ["", "AUDIO COMMANDS"]
    for name in ("aplay", "arecord", "amixer"):
        lines.append(f"{name}: {shutil.which(name) or 'not found'}")
    lines += ["", "ALSA CARDS", _text_file("/proc/asound/cards"), "", "ALSA VERSION", _text_file("/proc/asound/version"), "", "ALSA PCM STREAM CAPABILITIES"]
    cards = list_cards()
    if not cards:
        lines.append("(no ALSA cards detected)")
    for no, name in cards:
        lines.append(f"card {no}: {name}")
        st = parse_stream(no)
        for direction in ("Playback", "Capture"):
            if not st[direction]:
                lines.append(f"  {direction}: no stream0 capabilities parsed")
            for alt in st[direction]:
                lines.append(f"  {direction}: format={alt.fmt}; channels={alt.channels}; rates={','.join(map(str, alt.rates))}")
    lines += ["", "CURRENT PCM HW_PARAMS (only active streams appear)"]
    found = False
    for path in sorted(Path("/proc/asound").glob("card*/pcm*/sub*/hw_params")):
        val = _text_file(path)
        if val and val != "(нет)" and val.lower() != "closed":
            found = True
            lines.append(f"{path}: {val}")
    if not found:
        lines.append("No open PCM stream detected at report time. Open hw_params is meaningful only while stream is active.")
    lines += ["", "ALSA DEVICE LIST", _run(["aplay", "-l"]), "", "ALSA CAPTURE LIST", _run(["arecord", "-l"])]

    c = calib.load()
    duplex = list_duplex_devices()
    lines += ["", "ALSA FULL-DUPLEX DEVICES (playback ∩ capture)"]
    lines += [f"  {dev}: {label}" for dev, label in duplex] or ["  (none detected)"]
    configured_ok = c["device"] == "sim" or c["device"] in {dev for dev, _ in duplex}
    lines += ["", "VALVEBENCH CONFIG (calibration values intentionally summarized)",
              f"device={c['device']} fs={c['fs']} in_map={c['in_map']} out_map={c['out_map']}",
              f"configured_device_available={'yes' if configured_ok else 'NO — select a full-duplex device in the GUI'}",
              f"output_calibrated={[bool(v) for v in c['out_fs_vrms']]}; input_calibrated={[bool(v) for v in c['in_fs_vrms']]}",
              f"external_attenuator={c['att_db']} dB; mains={c['mains']} Hz; band_hi={c['band_hi']} Hz"]
    if include_selftest:
        lines += ["", "DSP / SIMULATOR SELF-TEST", format_selftest(run_selftest())]
    lines += ["", "NOTES", 
              "- This report does not start or stop physical audio streams.",
              "- ALSA stream0 lists advertised USB alternate settings; it does not prove the active PCM rate.",
              "- hw_params normally shows the actual PCM parameters only while a stream is open.",
              "- The simulator self-test checks software mathematics, not the analogue performance of the audio interface."]
    return "\n".join(lines)


def save_report(text):
    directory = Path.home() / ".local" / "state" / "valvebench" / "debug"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (time.strftime("debug-%Y%m%d-%H%M%S") + ".txt")
    path.write_text(text, encoding="utf-8")
    return path
