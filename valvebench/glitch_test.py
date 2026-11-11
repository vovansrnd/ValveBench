"""Локализация сбойных отсчётов и остановок потока: один тон (или тишина) без калибровки уровня.

Опыты, которые этим можно поставить (ничего не паять):
  A) петля ЦАП L → АЦП L, играет L            — исходное поведение;
  B) кабель снят, вход АЦП закорочен, играет L — если сбои остались, они возникают ВНУТРИ карты
     (цифровая связь ЦАП→АЦП), а не из аналогового тракта;
  C) играет L на −60 dBFS / −18 dBFS            — зависит ли частота сбоев от уровня данных ЦАП;
  D) ничего не играет (--channel none)          — сбои «в тишине» (источник — не тон);
  E) играет R, смотрим ADC L                    — влияет ли соседний канал.
Каждое ожидание «частота сбоев» сравнивается с тишиной до/после той же записи.

0.1.20: перенос идеи форка 0.1.11-alternative на детекторы ``livemetrics`` (вместо отсутствующего
``steady_events``). Отличия от форка: анализ идёт БЛОКАМИ по ~2.7 с с перекрытием (10-минутная
запись не требует гигабайт памяти); провалы несущей ищутся только на ведомом канале;
направление «пауза/потеря» не определяется (см. livemetrics.detect_phase_jumps); источник
сбоя в отчёте не называется — по картине USB, FIFO, I²S, DMA и буфер драйвера неразличимы;
XRUN читаются общим построчным разбором (мс и кадры).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from . import livemetrics as lm
from . import steady
from .backend import parse_alsa_log
from .pcm import StreamPcmReader, stream_to_float32_wav, to_pcm

GUARD_S = 0.5            # отступ от краёв устойчивого участка (fade, переходные процессы)
BLOCK_S = 2.7            # размер блока анализа
BLOCK_OVERLAP_S = 0.1    # перекрытие блоков; событие относится к блоку, в который попало его время
CLUSTER_GAP_S = 1.0      # события ближе этого интервала считаются одной группой


def _write_program(path: Path, fmt: str, fs: int, f: float, level_dbfs: float, channel: str,
                   seconds: float, idle_s: float, fade_s: float) -> float:
    amp = 10.0 ** (level_dbfs / 20.0)
    total_s = idle_s + fade_s + seconds + fade_s + idle_s
    total = int(round(total_s * fs))
    t_fade_in, t_start = idle_s, idle_s + fade_s
    t_end, t_fade_out_end = t_start + seconds, t_start + seconds + fade_s
    left = channel in ("L", "both")
    right = channel in ("R", "both")
    with path.open("wb") as fh:
        for s0 in range(0, total, fs):
            n = min(fs, total - s0)
            tt = (np.arange(s0, s0 + n)) / fs
            env = np.zeros(n)
            env[(tt >= t_start) & (tt < t_end)] = 1.0
            m = (tt >= t_fade_in) & (tt < t_start)
            env[m] = (tt[m] - t_fade_in) / fade_s
            m = (tt >= t_end) & (tt < t_fade_out_end)
            env[m] = 1.0 - (tt[m] - t_end) / fade_s
            if channel == "none":
                env[:] = 0.0
            chunk = steady._tone_chunk(s0, n, fs, f, amp, left, right, fade=env)
            fh.write(to_pcm(chunk, fmt))
    return total_s


def clusters(times, gap=CLUSTER_GAP_S):
    """Число групп событий: новая группа начинается после паузы длиннее gap секунд."""
    t = sorted(times)
    return 0 if not t else 1 + sum(1 for a, b in zip(t, t[1:]) if b - a > gap)


class _ArraySource:
    """Источник кадров поверх готового массива (тесты, короткие записи)."""

    def __init__(self, x):
        self.x = np.asarray(x)
        self.frames = self.x.shape[0]

    def read_frames(self, start, count):
        return self.x[start:start + count, :2]


class _FileSource:
    """Источник кадров поверх raw-файла: читает только запрошенное окно (первые два канала)."""

    def __init__(self, path, fmt, channels):
        self.reader = StreamPcmReader(path, fmt, channels)
        self.frames = self.reader.frames

    def read_frames(self, start, count):
        return self.reader.read_frames(start, count)[:, :2]


def scan_range(src, a_frame, b_frame, fs, f0, driven, t0, block_s=BLOCK_S, overlap_s=BLOCK_OVERLAP_S):
    """События на участке [a_frame, b_frame) источника: список словарей с временем t0 + t.

    Читается и анализируется по одному блоку с перекрытием, поэтому память не зависит от длины
    записи (пик ≈ несколько десятков МБ). f0 — частота ведущего тона (None — тишина); driven —
    (L, R) ведомые каналы. Событие принадлежит блоку, в который попало его время."""
    B = int(round(block_s * fs))
    ov = int(round(overlap_s * fs))
    n = b_frame - a_frame
    out = []
    for s in range(0, n, B):
        a, b = max(0, s - ov), min(n, s + B + ov)
        if b - a < int(0.3 * fs):
            continue
        x = src.read_frames(a_frame + a, b - a)
        res = lm.frame_integrity(x, fs, f0, driven, hf_lo=40000.0)
        off = a / fs
        lo, hi = s / fs, min(n, s + B) / fs
        for ci in res:
            def keep(t_rel):
                return lo <= off + t_rel < hi
            for e in ci.dropouts:
                if keep(e["start_s"]):
                    out.append({"time_s": t0 + off + e["start_s"], "adc": ci.name, "class": "dropout",
                                "level_dbfs": e["depth_db"], "error_lsb24": "", "duration_ms": e["dur_s"] * 1e3, "sign": 0})
            for e in ci.phase_jumps:
                if keep(e["t_s"]):
                    out.append({"time_s": t0 + off + e["t_s"], "adc": ci.name, "class": "phase_jump",
                                "level_dbfs": "", "error_lsb24": "", "duration_ms": "", "sign": 0,
                                "jump_deg": e["jump_deg"]})
            for e in ci.glitches:
                if keep(e["t_s"]):
                    out.append({"time_s": t0 + off + e["t_s"], "adc": ci.name, "class": e["kind"],
                                "level_dbfs": e["amp_dbfs"], "error_lsb24": e["amp"] * 2 ** 23,
                                "duration_ms": e["width"] / fs * 1e3, "sign": e.get("sign", 0)})
            for e in ci.bursts:
                if keep(e["start_s"]):
                    out.append({"time_s": t0 + off + e["start_s"], "adc": ci.name, "class": "burst",
                                "level_dbfs": "", "error_lsb24": "", "duration_ms": e["dur_s"] * 1e3, "sign": 0})
    return out


def scan_segment(x, fs, f0, driven, t0, block_s=BLOCK_S, overlap_s=BLOCK_OVERLAP_S):
    """То же для массива x (n, 2) — обёртка над scan_range."""
    src = _ArraySource(x)
    return scan_range(src, 0, src.frames, fs, f0, driven, t0, block_s, overlap_s)


def run_glitch_test(play_dev: str, rec_dev: str, fs: int = 192000, channel: str = "L",
                    tone_level_dbfs: float = -18.0, seconds: float = 60.0, tone_hz: float = 997.0,
                    idle_s: float = 6.0, fade_s: float = 1.0, out_root: str | Path = "emu_measurements",
                    keep_wav: bool = False, print_fn=print) -> Path:
    if channel not in ("L", "R", "both", "none"):
        raise ValueError("channel: L, R, both или none")
    if not (10.0 <= seconds <= 600.0):
        raise ValueError("seconds должно быть 10–600")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = Path(out_root) / f"glitch-{channel}-{stamp}"
    outdir.mkdir(parents=True, exist_ok=False)
    play = steady.choose_device_format(play_dev, fs, "playback", "auto")
    capture = steady.choose_device_format(rec_dev, fs, "capture", "auto")
    print_fn(f"Проверяю ALSA: playback {play_dev} ({play.fmt}), capture {rec_dev} ({capture.fmt}), {fs} Гц; "
             f"играет: {channel}, {tone_level_dbfs:.1f} dBFS, {seconds:.0f} с")
    play_file, cap_file = outdir / "play.raw", outdir / "capture.raw"
    total_s = _write_program(play_file, play.fmt, fs, tone_hz, tone_level_dbfs, channel, seconds, idle_s, fade_s)
    lead = 1.0
    cap_s = total_s + lead + 2.0
    io = steady._capture_and_play(play_file, cap_file, play, capture, fs, cap_s, lead_seconds=lead)
    # Файл НЕ читается целиком: 10 минут при 192 кГц и S24_3LE — это 659 МиБ файла и 1.7 ГиБ float64.
    src = _FileSource(cap_file, capture.fmt, capture.channels)
    xr = _read_xruns(io)
    try:
        return analyse_recording(src, fs, channel, tone_level_dbfs, seconds, tone_hz, idle_s, fade_s, lead,
                                 total_s, xr, outdir, play_dev, rec_dev, play.fmt, capture.fmt, False, print_fn,
                                 raw_path=cap_file if keep_wav else None, raw_fmt=capture.fmt,
                                 raw_channels=capture.channels)
    finally:
        play_file.unlink(missing_ok=True)
        cap_file.unlink(missing_ok=True)


def _read_xruns(io: dict) -> dict:
    """XRUN из stderr обоих процессов: {'playback': [...], 'capture': [...]} (мс, кадры, текст)."""
    out = {"playback": [], "capture": []}
    for key, name in (("playback", "playback_stderr"), ("capture", "capture_stderr")):
        try:
            text = Path(io[name]).read_text(errors="replace")
        except (OSError, KeyError):
            continue
        events, _rest = parse_alsa_log(text if text.endswith("\n") else text + "\n")
        out[key] = [{"kind": "underrun" if key == "playback" else "overrun", "ms": e["dur_ms"],
                     "frames": e["dur_frames"], "text": e["text"]} for e in events]
    return out


def analyse_recording(raw, fs, channel, tone_level_dbfs, seconds, tone_hz, idle_s, fade_s, lead, total_s,
                      xr, outdir, play_dev="", rec_dev="", play_fmt="", cap_fmt="", keep_wav=False,
                      print_fn=print, raw_path=None, raw_fmt=None, raw_channels=2) -> Path:
    """Анализ готовой записи (отделён от ввода-вывода: проверяется на синтетике без ALSA).

    raw — массив (n, ≥2) или источник кадров с методами ``frames`` / ``read_frames(start, count)``
    (файловый источник читает окнами, целиком запись в память не загружается)."""
    outdir = Path(outdir)
    src = _ArraySource(raw) if isinstance(raw, np.ndarray) else raw
    tone_a = lead + idle_s + fade_s + GUARD_S
    tone_b = lead + idle_s + fade_s + seconds - GUARD_S
    idle_a = (lead + 0.5, lead + idle_s - 0.5)
    idle_b = (lead + total_s - idle_s + fade_s + 0.5, lead + total_s - 0.5)

    def fr(r):
        return int(r[0] * fs), min(int(r[1] * fs), src.frames)
    res = {"channel": channel, "level_dbfs": tone_level_dbfs, "fs": fs, "seconds": seconds,
           "xrun": xr, "adc": {}}
    driven_ch = None if channel == "none" else (0 if channel in ("L", "both") else 1)
    driven = (channel in ("L", "both"), channel in ("R", "both"))
    f_used = None
    if channel != "none":
        a0, a1 = fr((tone_a, min(tone_b, tone_a + 4.0)))
        seg = src.read_frames(a0, min(a1 - a0, 1 << 17))[:, driven_ch]
        f_used = lm.refine_tone(seg, fs, tone_hz)[0] if seg.size >= (1 << 15) else tone_hz
    t_lo, t_hi = fr((tone_a, tone_b))
    events = scan_range(src, t_lo, t_hi, fs, f_used, driven, tone_a)
    idle_events = []
    for r in (idle_a, idle_b):
        i_lo, i_hi = fr(r)
        idle_events += scan_range(src, i_lo, i_hi, fs, None, (False, False), r[0])
    minutes = max((tone_b - tone_a) / 60.0, 1e-9)
    idle_min = ((idle_a[1] - idle_a[0]) + (idle_b[1] - idle_b[0])) / 60.0
    xr_all = [("playback", x) for x in xr["playback"]] + [("capture", x) for x in xr["capture"]]
    report = ["VALVEBENCH GLITCH / STALL TEST",
              f"Playback: {play_dev} ({play_fmt}); capture: {rec_dev} ({cap_fmt}); Fs={fs} Hz",
              f"Играет: {channel} · тон {tone_hz:g} Гц / {tone_level_dbfs:.1f} dBFS · {seconds:.0f} с (минус {GUARD_S:g} с у краёв)",
              "ALSA XRUN: " + ("; ".join(f"{k} {x['kind']}" + (f" ≥{x['ms']:.1f} мс" if x.get('ms') else
                                                              f" ≥{x['frames']:.0f} кадров" if x.get('frames') else "")
                               for k, x in xr_all) if xr_all else "aplay/arecord не сообщили о пропусках")]
    for ch in ("L", "R"):
        ev = [e for e in events if e["adc"] == ch]
        idle = [e for e in idle_events if e["adc"] == ch]
        gl = [e for e in ev if e["class"] in ("single_sample", "short")]
        st = [e for e in ev if e["class"] == "dropout"]
        pj = [e for e in ev if e["class"] == "phase_jump"]
        bu = [e for e in ev if e["class"] == "burst"]
        idle_gl = [e for e in idle if e["class"] in ("single_sample", "short")]
        a = {"dropouts": [{"time_s": e["time_s"], "duration_ms": e["duration_ms"], "depth_db": e["level_dbfs"]} for e in st],
             "phase_jumps": [{"time_s": e["time_s"], "jump_deg": e.get("jump_deg")} for e in pj],
             "bursts": len(bu), "glitches": len(gl), "glitches_per_minute": len(gl) / minutes,
             "single_sample": sum(1 for e in gl if e["class"] == "single_sample"),
             "glitch_clusters": clusters([e["time_s"] for e in gl]),
             "idle_glitches": len(idle_gl), "idle_glitches_per_minute": len(idle_gl) / max(idle_min, 1e-9),
             "level_dbfs_median": float(np.median([e["level_dbfs"] for e in gl])) if gl else None,
             "lsb_median": float(np.median([abs(e["error_lsb24"]) for e in gl])) if gl else None,
             "positive": sum(1 for e in gl if e["sign"] > 0), "negative": sum(1 for e in gl if e["sign"] < 0)}
        res["adc"][ch] = a
        skip_drop = (channel == "none") or (driven_ch is not None and ch != "LR"[driven_ch] and channel != "both")
        line = f"ADC {ch}: "
        if skip_drop:
            line += "провалы несущей не ищутся (канал не ведомый); "
        else:
            line += f"провалов несущей {len(st)}"
            if st:
                line += " (" + ", ".join(f"{s['duration_ms']:.0f} мс@{s['time_s']:.2f}с" for s in a["dropouts"]) + ")"
            line += f", скачков фазы {len(pj)}; "
        line += (f"сбойных отсчётов {a['glitches']} (из них одиночных {a['single_sample']}; "
                 f"{a['glitches_per_minute']:.1f}/мин, групп {a['glitch_clusters']}); всплесков {len(bu)}")
        if gl:
            line += (f"; медиана {a['level_dbfs_median']:.1f} dBFS = {a['lsb_median']:.0f} LSB(24), "
                     f"знаки +{a['positive']}/−{a['negative']}")
        line += f"; в тишине до/после: {len(idle_gl)} ({a['idle_glitches_per_minute']:.1f}/мин)"
        report.append(line)
    report.append("Сравнивай ЧАСТОТУ сбоев между опытами (A…E), а не отдельные числа; тишина той же записи — контроль. "
                  "Источник по картине не определяется: USB, FIFO, I²S, DMA и буфер драйвера неразличимы.")
    (outdir / "summary.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    (outdir / "glitch.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    with (outdir / "events.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time_s", "adc", "class", "level_dbfs", "error_lsb24", "duration_ms", "sign"])
        for e in sorted(events, key=lambda e: e["time_s"]):
            w.writerow([f"{e['time_s']:.5f}", e["adc"], e["class"], e["level_dbfs"], e["error_lsb24"],
                        e["duration_ms"], e["sign"]])
    if keep_wav and raw_path is not None:
        stream_to_float32_wav(raw_path, raw_fmt, raw_channels, outdir / "capture-float32.wav", fs)   # одним проходом, блоками
    elif keep_wav and isinstance(raw, np.ndarray):
        wavfile.write(str(outdir / "capture-float32.wav"), fs, raw.astype(np.float32))
    for ln in report:
        print_fn(ln)
    print_fn(f"\nРезультаты сохранены: {outdir}")
    return outdir
