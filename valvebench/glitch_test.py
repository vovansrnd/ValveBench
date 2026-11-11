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
BLOCK_OVERLAP_S = 0.1    # перекрытие блоков для устойчивого обнаружения событий на границах
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

    PCM читается и анализируется по одному блоку, а не целиком. Для поиска длинных провалов
    сохраняется только огибающая несущей с шагом 1 мс (порядка 20 МиБ на 10 минут для двух
    ведомых каналов, плюс временные массивы), а не исходный float64-поток (около 1.7 ГиБ).
    f0 — частота ведущего тона (None — тишина); driven —
    (L, R) ведомые каналы. Точечные события закрепляются за непересекающимся ядром блока;
    dropout восстанавливается по непрерывной огибающей, а длительные фрагменты объединяются."""
    B = int(round(block_s * fs))
    ov = int(round(overlap_s * fs))
    n = b_frame - a_frame
    out = []
    # The generic frame detector uses a per-window median to decide whether a
    # carrier exists. A dropout occupying most of one 2.7 s window can make
    # that median collapse, hiding the recovery edge. For this controlled
    # steady-tone test we keep a reference from the first stable block and
    # assemble a continuous 1 ms envelope track across owned block intervals.
    carrier_ref = [None, None]
    carrier_times = [[], []]
    carrier_envs = [[], []]
    for s in range(0, n, B):
        a, b = max(0, s - ov), min(n, s + B + ov)
        if b - a < int(0.3 * fs):
            continue
        x = src.read_frames(a_frame + a, b - a)
        res = lm.frame_integrity(x, fs, f0, driven, hf_lo=40000.0)
        off = a / fs
        lo, hi = s / fs, min(n, s + B) / fs
        for c, ci in enumerate(res):
            if f0 is not None and driven[c]:
                # Normally reuse the exact track computed by frame_integrity.
                # Only a window whose median was swallowed by a long dropout
                # needs a separate envelope pass; that is the exceptional case.
                if ci.carrier_t is not None and ci.carrier_env is not None:
                    et, env = ci.carrier_t, ci.carrier_env
                else:
                    et, env, _win = lm.carrier_track(x[:, c], fs, f0, step_s=0.001)
                gt = off + et
                owned = (gt >= lo) & (gt < hi)
                if owned.any():
                    tv, ev = gt[owned], np.asarray(env[owned], dtype=float)
                    if carrier_ref[c] is None:
                        # The test tone is steady after the guard interval. The
                        # median of the first stable block is a robust reference;
                        # a later good block can establish it if the run begins
                        # with a complete interruption.
                        ref = float(np.median(ev)) if ev.size else 0.0
                        if ref >= 10.0 ** (-70.0 / 20.0):
                            carrier_ref[c] = ref
                    carrier_times[c].append(tv)
                    carrier_envs[c].append(ev)
            def keep_point(t_rel):
                return lo <= off + t_rel < hi
            def keep_span(start_s, dur_s):
                # Use the interval midpoint for ownership. A neighboring
                # overlapped window may contain a longer fragment, which is
                # merged below into the full duration.
                return keep_point(start_s + 0.5 * max(0.0, dur_s))
            for e in ci.phase_jumps:
                if keep_point(e["t_s"]):
                    out.append({"time_s": t0 + off + e["t_s"], "adc": ci.name, "class": "phase_jump",
                                "level_dbfs": "", "error_lsb24": "", "duration_ms": "", "sign": 0,
                                "jump_deg": e["jump_deg"]})
            for e in ci.glitches:
                if keep_point(e["t_s"]):
                    out.append({"time_s": t0 + off + e["t_s"], "adc": ci.name, "class": e["kind"],
                                "level_dbfs": e["amp_dbfs"], "error_lsb24": e["amp"] * 2 ** 23,
                                "duration_ms": e["width"] / fs * 1e3, "sign": e.get("sign", 0)})
            for e in ci.bursts:
                if keep_span(e["start_s"], e["dur_s"]):
                    out.append({"time_s": t0 + off + e["start_s"], "adc": ci.name, "class": "burst",
                                "level_dbfs": "", "error_lsb24": "", "duration_ms": e["dur_s"] * 1e3, "sign": 0})
    # Reconstruct carrier dropouts from the continuous envelope, using the
    # reference established before a long event. This detects intervals longer
    # than a single analysis block and yields edges rather than block-sized
    # overestimates.
    env_step = 0.001
    min_s = float(getattr(lm, "DROPOUT_MIN_S", 0.002))
    threshold = 10.0 ** (-abs(float(getattr(lm, "DROPOUT_DEPTH_DB", 3.0))) / 20.0)
    for c in (0, 1):
        if not driven[c]:
            continue
        if carrier_ref[c] is None or not carrier_times[c]:
            # Do not report "zero dropouts" when no reference carrier was
            # captured at all. This is an invalid/unverifiable test segment,
            # not evidence that the stream was continuous.
            out.append({"time_s": t0, "adc": "LR"[c], "class": "carrier_unverified",
                        "level_dbfs": "", "error_lsb24": "", "duration_ms": n / fs * 1e3, "sign": 0})
            continue
        tt = np.concatenate(carrier_times[c])
        ee = np.concatenate(carrier_envs[c])
        order = np.argsort(tt)
        tt, ee = tt[order], ee[order]
        # Core ownership intervals should be disjoint. Remove any duplicate
        # grid points defensively in case rounding differs by one sample.
        if tt.size > 1:
            unique = np.r_[True, np.diff(tt) > 0.5 * env_step]
            tt, ee = tt[unique], ee[unique]
        ratio = np.maximum(ee, 1e-12) / float(carrier_ref[c])
        db = np.maximum(20.0 * np.log10(ratio), -120.0)
        bad = db < -abs(float(getattr(lm, "DROPOUT_DEPTH_DB", 3.0)))
        idx = np.flatnonzero(bad)
        if not idx.size:
            continue
        runs = []
        a0 = p0 = int(idx[0])
        for ix in idx[1:]:
            ix = int(ix)
            if ix > p0 + 1:
                if (p0 - a0 + 1) * env_step >= min_s:
                    runs.append((a0, p0))
                a0 = ix
            p0 = ix
        if (p0 - a0 + 1) * env_step >= min_s:
            runs.append((a0, p0))
        for a0, b0 in runs:
            out.append({"time_s": t0 + max(0.0, float(tt[a0]) - env_step / 2.0),
                        "adc": "LR"[c], "class": "dropout", "level_dbfs": float(np.min(db[a0:b0 + 1])),
                        "error_lsb24": "", "duration_ms": (b0 - a0 + 1) * env_step * 1e3, "sign": 0})
    # A long event can be clipped at both ends of consecutive analysis
    # windows. Merge overlapping fragments with the same ADC and class.
    merged = []
    for e in sorted(out, key=lambda v: (v["adc"], v["class"], v["time_s"])):
        if e["class"] not in ("dropout", "burst") or not isinstance(e.get("duration_ms"), (int, float)):
            merged.append(dict(e))
            continue
        cur = dict(e)
        start = float(cur["time_s"])
        end = start + max(0.0, float(cur["duration_ms"])) / 1000.0
        if merged:
            prev = merged[-1]
            same = prev.get("adc") == cur.get("adc") and prev.get("class") == cur.get("class")
            if same and prev["class"] in ("dropout", "burst"):
                pstart = float(prev["time_s"])
                pend = pstart + max(0.0, float(prev.get("duration_ms") or 0.0)) / 1000.0
                if start <= pend + 2.0 / fs:
                    new_start, new_end = min(start, pstart), max(end, pend)
                    prev["time_s"] = new_start
                    prev["duration_ms"] = (new_end - new_start) * 1000.0
                    if cur["class"] == "dropout":
                        vals = [v for v in (prev.get("level_dbfs"), cur.get("level_dbfs"))
                                if isinstance(v, (int, float))]
                        if vals:
                            prev["level_dbfs"] = min(vals)
                    continue
        merged.append(cur)
    return sorted(merged, key=lambda v: v["time_s"])


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
    try:
        io = steady._capture_and_play(play_file, cap_file, play, capture, fs, cap_s, lead_seconds=lead)
        # Файл НЕ читается целиком: 10 минут при 192 кГц и S24_3LE — это 659 МиБ файла и 1.7 ГиБ float64.
        src = _FileSource(cap_file, capture.fmt, capture.channels)
        xr = _read_xruns(io)
        return analyse_recording(src, fs, channel, tone_level_dbfs, seconds, tone_hz, idle_s, fade_s, lead,
                                 total_s, xr, outdir, play_dev, rec_dev, play.fmt, capture.fmt, keep_wav, print_fn,
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
        carrier_unverified = any(e["class"] == "carrier_unverified" for e in ev)
        pj = [e for e in ev if e["class"] == "phase_jump"]
        bu = [e for e in ev if e["class"] == "burst"]
        idle_gl = [e for e in idle if e["class"] in ("single_sample", "short")]
        a = {"dropouts": [{"time_s": e["time_s"], "duration_ms": e["duration_ms"], "depth_db": e["level_dbfs"]} for e in st],
             "carrier_reference": "missing" if carrier_unverified else ("present" if channel != "none" else "not_applicable"),
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
        elif carrier_unverified:
            line += "несущая не обнаружена на контрольном участке — проверка провалов недостоверна; "
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
