#!/usr/bin/env python3
"""v2.5 — маркер-якорный A/B через настоящий AlsaCli.play_record (§13).

2×2 условия: начальная тишина 0/1.5 с × конечная 0/0.6 с, чередующийся
порядок (A-B-B-A × 2 раунда), ≥2 валидных повтора на условие.
Перед КАЖДЫМ прогоном — одинаковая подготовка: playback 1 кГц −30 dBFS
(clock-preflight replicate) + фиксированная пауза (стандартизация состояния
D10, §13.4.3). Программа с точными кадровыми позициями: [lead][chirp A
6.5→7.5к][gap 0.15][тон 997 4.55 с, фейды как Rig._seg][trailing][chirp Б
10.5→11.5к][tail 1.0].

Инструментирование наблюдателя (production не изменяется): subprocess.Popen
и subprocess.run в модуле backend перехватываются на время вызова
play_record — сохраняются РЕАЛЬНЫЕ команды, rc и stderr обоих процессов
(вместо реконструкции, ошибку с которой нашёл координатор в v2.4).

Детекция: валидированный v2.1 согласованный фильтр (все локальные максимумы,
глобальный максимум = событие) для обоих чирпов независимо в L/R;
классификация тона 997 против остатка 1000 Гц по 0.5-с chunk-FFT
(периодический тон — неоднозначный якорь, поэтому частотная классификация);
огибающая 1 мс; пороги −45/−55/−65 dBFS с правилом устойчивости 20 мс,
поиск ПОСЛЕ начала тона; реплей production Rig._onset/_tone_an с честным
учётом dsp._blocks.
"""
import json
import math
import os
import subprocess as real_subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli, SimBackend, to_pcm  # noqa: E402
from valvebench import backend as backend_mod  # noqa: E402

DAC_USBID = "152a:85dd"   # SMSL D10
ADC_USBID = "041e:3f04"   # E-MU 0404
FS = 192000
PAD = int(0.05 * FS)
OUT = ROOT / "measurements" / "diagnostics" / "v25"
PRE_TONE = 1.0           # 1 кГц, −30 dBFS — реплика clock-preflight
PRE_IDLE_S = 1.5         # фиксированная пауза после preflight


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


def chirp(fs, dur, f0, f1, level_dbfs=-30.0):
    n = int(round(dur * fs))
    t = np.arange(n) / fs
    k = (f1 - f0) / dur
    ph = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    s = 10 ** (level_dbfs / 20) * np.sin(ph)
    fade = int(0.01 * fs)
    s[:fade] *= np.linspace(0, 1, fade)
    s[-fade:] *= np.linspace(1, 0, fade)
    return s


def demod_env(x, fs, f, win_s=0.001):
    n = len(x)
    t = np.arange(n) / fs
    z = x * np.exp(-2j * np.pi * f * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(win_s * fs)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    return 2 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))


def xcorr_markers(arr, template, fs):
    """v2.1-детектор: глобальный максимум + ВСЕ локальные максимумы
    (разнос ≥ длина шаблона), без пропусков интервалов."""
    n = len(arr); m = len(template)
    L = int(2 ** np.ceil(np.log2(n + m)))
    F = np.fft.rfft(arr, L) * np.conj(np.fft.rfft(template, L))
    cc = np.fft.irfft(F, L)[: n - m + 1]
    energy = np.convolve(arr ** 2, np.ones(m), "valid").clip(1e-30)
    cc = cc[: len(energy)] / (np.linalg.norm(template) * np.sqrt(energy))
    gmax = int(np.argmax(cc)); gscore = float(cc[gmax])
    pk, props = find_peaks(cc, height=0.15, distance=m)
    maxima = [(int(p), float(h)) for p, h in zip(pk, props["peak_heights"])]
    if not any(abs(p - gmax) < m // 2 for p, _ in maxima):
        maxima.append((gmax, gscore))
    maxima.sort(key=lambda t: -t[1])
    return {"global": (gmax, gscore), "maxima": maxima}


class SubprocessObserver:
    """Наблюдатель РЕАЛЬНЫХ команд/rc/stderr backend-процессов на время
    play_record. Production не изменяется — патч действует только внутри
    контекста и восстанавливается."""
    def __init__(self, logdir):
        self.logdir = logdir
        self.records = []
        self._orig_popen = backend_mod.subprocess.Popen
        self._orig_run = backend_mod.subprocess.run

    def __enter__(self):
        obs = self
        mod = backend_mod.subprocess

        def patched_popen(*args, **kwargs):
            rec = {"cmd": list(args[0]) if args else None,
                   "stderr_path": None, "stdout_path": None, "popen": None}
            idx = len(obs.records)
            # подменяем ТОЛЬКО stderr (production читает его через
            # _read_err(f.seek(0); f.read()) — интерфейс совместим).
            # stdout НЕ трогаем: production читает capture из своего
            # временного файла — подмена оставляла его пустым.
            stderr_arg = kwargs.get("stderr")
            if stderr_arg is not None and hasattr(stderr_arg, "seek"):
                p = os.path.join(obs.logdir, f"stderr_arecord_{idx}.txt")
                kwargs["stderr"] = open(p, "w+b")
                rec["stderr_path"] = p
            popen = obs._orig_popen(*args, **kwargs)
            rec["popen"] = popen
            obs.records.append(rec)
            return popen

        def patched_run(*args, **kwargs):
            rec = {"cmd": list(args[0]) if args else None, "rc": None}
            result = obs._orig_run(*args, **kwargs)
            rec["rc"] = result.returncode
            if result.stderr is not None:
                rec["stderr_text"] = result.stderr.decode("utf-8", "replace")[:500]
            obs.records.append(rec)
            return result

        mod.Popen = patched_popen
        mod.run = patched_run
        return self

    def __exit__(self, *a):
        backend_mod.subprocess.Popen = self._orig_popen
        backend_mod.subprocess.run = self._orig_run
        for r in self.records:
            if r.get("popen") is not None:
                try:
                    r["rc"] = r["popen"].returncode
                except (ValueError, AttributeError):
                    r["rc"] = None
                sp = r.get("stderr_path")
                if sp and os.path.exists(sp):
                    r["stderr_text"] = open(sp, "rb").read().decode(
                        "utf-8", "replace")[:500]


def build_program(fs, lead_s, trailing_s):
    """Программа с точными кадровыми позициями (кадры в базе программы)."""
    P = []
    pos = {}
    cur = [0]
    def seg(dur, sig=None, name=None):
        n = int(round(dur * fs))
        if name:
            pos[name + "_start"] = cur[0]
        P.append(np.zeros((n, 2)) if sig is None else sig)
        cur[0] += len(P[-1])
        if name:
            pos[name + "_end"] = cur[0]
    if lead_s:
        seg(lead_s, name="lead")
    seg(0.08, np.column_stack([chirp(fs, 0.08, 6500, 7500)] * 2), name="marker_A")
    seg(0.15, name="gap1")
    nt = int(round(4.55 * fs))
    tone = 10 ** (-30 / 20) * np.sin(2 * np.pi * 997 * np.arange(nt) / fs)
    fade = int(0.01 * fs)
    tone[:fade] *= np.linspace(0, 1, fade)
    tone[-fade:] *= np.linspace(1, 0, fade)
    seg(4.55, np.column_stack([tone, tone]), name="tone")
    if trailing_s:
        seg(trailing_s, name="trailing")
    seg(0.08, np.column_stack([chirp(fs, 0.08, 10500, 11500)] * 2), name="marker_B")
    seg(1.0, name="tail")
    return np.concatenate(P), pos


def main():
    dac, adc = find_card(DAC_USBID), find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
    OUT = ROOT / "measurements" / "diagnostics" / "v25"
    OUT.mkdir(parents=True, exist_ok=True)

    # доказательство устройств: mapping + stream0
    stream0 = {}
    for usbid, card in ((DAC_USBID, dac), (ADC_USBID, adc)):
        p = f"/proc/asound/card{card}/stream0"
        stream0[usbid] = {"card": card,
                          "content": open(p).read()[:1500] if os.path.exists(p) else None}
    mapping = {"D10": {"usbid": DAC_USBID, "card": dac, "hw": hw_dac},
               "E-MU": {"usbid": ADC_USBID, "card": adc, "hw": hw_adc}}
    json.dump({"mapping": mapping, "stream0": stream0},
              open(OUT / "device-mapping.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("mapping:", mapping, flush=True)

    cfg = calib.Calibration()
    be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc,
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    fmt_facts = {"fmt_out": be.fmt_out, "fmt_in": be.fmt_in,
                 "ch_out": be.ch_out, "ch_in": be.ch_in,
                 "buffer_ms": be.buffer_ms}
    print("fmt фактические:", fmt_facts, flush=True)

    rig = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
    k1, a1 = rig.amp_tone(1000.0, -30.0)   # реплика clock-preflight
    preflight, _ = rig._seg([(k1, a1)], [(k1, a1)], 0.4)

    tmplA = chirp(FS, 0.08, 6500, 7500)
    tmplB = chirp(FS, 0.08, 10500, 11500)
    rig_n = FS                              # df=1.0 → блок 1.0 с

    # 2×2 в чередующемся порядке: (lead, trailing) — A-B-B-A, затем повтор
    order = [(0.0, 0.0), (1.5, 0.0), (1.5, 0.0), (0.0, 0.0),
             (0.0, 0.6), (1.5, 0.6), (1.5, 0.6), (0.0, 0.6)]
    summary = []
    for idx, (lead_s, trail_s) in enumerate(order):
        cond = f"L{lead_s:g}_T{trail_s:g}_r{1 if idx < 4 else 2}"
        print(f"\n=== прогон {idx+1}/8: lead={lead_s} trailing={trail_s} ({cond})",
              flush=True)
        # подготовка: идентичный 1 кГц preflight + фиксированная пауза
        be.play_record(preflight.copy())
        time.sleep(PRE_IDLE_S)

        prog, pos = build_program(FS, lead_s, trail_s)
        obs = SubprocessObserver(str(OUT))
        with obs:
            rec = be.play_record(prog.copy())
        # после выхода из контекста: rc и stderr собраны наблюдателем
        observer_meta = []
        for r in obs.records:
            entry = {"cmd": r["cmd"]}
            if r.get("stderr_path"):
                entry["stderr_file"] = os.path.basename(r["stderr_path"])
            if "rc" in r:
                entry["rc"] = r["rc"]
            if r.get("stderr_text"):
                entry["stderr_text"] = r["stderr_text"]
            observer_meta.append(entry)

        # артефакты
        wavfile.write(OUT / f"v25-stimulus-{cond}.wav", FS, prog.astype(np.float32))
        wavfile.write(OUT / f"v25-capture-{cond}.wav", FS, rec.astype(np.float32))
        pcm_sent = to_pcm(np.vstack([np.zeros((PAD, 2)), prog,
                                     np.zeros((PAD, 2))]), be.fmt_out)
        (OUT / f"v25-pcm-{cond}.bin").write_bytes(
            pcm_bytes if isinstance(pcm_bytes := pcm_sent, (bytes, bytearray))
            else pcm_sent.tobytes())

        capL, capR = rec[:, 0].astype(np.float64), rec[:, 1].astype(np.float64)
        n_cap = len(rec)
        ev = {}
        for name, arr in (("L", capL), ("R", capR)):
            xa = xcorr_markers(arr, tmplA, FS)
            xb = xcorr_markers(arr, tmplB, FS)
            ev[f"marker_A_{name}"] = {"global_abs": xa["global"][0],
                                      "score": round(xa["global"][1], 4),
                                      "maxima": [(p, round(s, 3)) for p, s in xa["maxima"]]}
            ev[f"marker_B_{name}"] = {"global_abs": xb["global"][0],
                                      "score": round(xb["global"][1], 4),
                                      "maxima": [(p, round(s, 3)) for p, s in xb["maxima"]]}
        mA = ev["marker_A_R"]["global_abs"]
        start_offset = mA - (pos["marker_A_start"] + PAD)
        exp = {"marker_A": pos["marker_A_start"] + PAD + start_offset,
               "tone_start": pos["tone_start"] + PAD + start_offset,
               "tone_end": pos["tone_end"] + PAD + start_offset,
               "marker_B": pos["marker_B_start"] + PAD + start_offset}
        ev["expected_abs"] = exp
        ev["start_offset_s"] = round(start_offset / FS, 5)
        ev["marker_B_residual_ms"] = round(
            (exp["marker_B"] - ev["marker_B_R"]["global_abs"]) * 1000 / FS, 2)

        # классификация 997 vs 1000 по 0.5-с chunk-FFT (тон vs остаток preflight)
        cls = []
        for i in range(0, n_cap - FS // 2, FS // 2):
            seg = capR[i:i + FS // 2]
            w = np.hanning(len(seg))
            X = np.abs(np.fft.rfft(seg * w))
            fr = np.fft.rfftfreq(len(seg), 1 / FS)
            f997 = float(np.max(X[(fr > 991) & (fr < 999)]))
            f1000 = float(np.max(X[(fr > 996) & (fr < 1004)]))
            cls.append({"t": round(i / FS, 2),
                        "f997_db": round(20 * np.log10(max(f997, 1e-12)), 1),
                        "f1000_db": round(20 * np.log10(max(f1000, 1e-12)), 1)})
        ev["chunk_class_997_1000"] = cls

        # огибающая 997 (1 мс) + пороги; поиск ПОСЛЕ начала тона
        env = demod_env(capR, FS, 997)
        tone_first = None
        ts0 = exp["tone_start"]
        above = np.where(env[ts0:ts0 + int(0.3 * FS)] > 10 ** (-45 / 20))[0]
        if len(above):
            tone_first = int(above[0] + ts0)
        drops = {}
        if tone_first is not None:
            stop = min(exp["marker_B"], n_cap)
            for tdb in (-45, -55, -65):
                thr = 10 ** (tdb / 20)
                below = env < thr
                d = None
                for i in range(tone_first, stop - int(0.02 * FS)):
                    if below[i:i + int(0.02 * FS)].all():
                        d = i
                        break
                drops[f"below_{abs(tdb)}"] = {
                    "frame": d,
                    "t_s": round(d / FS, 4) if d is not None else None,
                    "rel_expected_tone_end_s": round(
                        (d - exp["tone_end"]) / FS, 4) if d is not None else None,
                    "rel_markerB_s": round((d - exp["marker_B"]) / FS, 4)
                    if d is not None else None}
        ev["tone_env"] = {"tone_first_above_minus45": tone_first, "drops": drops}

        # pre-tone: остаточный 997/1000 до маркера A (загрязнение?)
        pre0, pre1 = int(0.3 * FS), max(int(0.3 * FS), mA - int(0.1 * FS))
        if pre1 > pre0:
            e_pre = demod_env(capR[pre0:pre1], FS, 997)
            ev["pre_tone_max_env997_db"] = round(
                20 * np.log10(max(float(np.max(e_pre)), 1e-12)), 1)

        # production реплей: onset + THD+N (unchanged Rig._onset/_tone_an)
        rig2 = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
        ev["production_replay"] = {"onset": None}
        try:
            onset = rig2._onset(rec, tone_hz=997.0)
        except Exception as exc:
            ev["production_replay"]["onset_error"] = f"{type(exc).__name__}: {str(exc)[:60]}"
            onset = None
        if onset is not None:
            settle_f = int(0.5 * FS)
            w0 = onset + settle_f
            blocks_full = 4
            used = blocks_full * rig_n
            w1 = w0 + used
            kk, _ = rig2.amp_tone(997.0, -30.0)
            thd = {}
            for c, ch in ((0, "L"), (1, "R")):
                r = rig2._tone_an(rec[w0:w1], c, kk, 9)
                thd[ch] = {"thdn_pct": round(r["thdn_pct"], 3),
                           "residual_dbfs": round(r["level_dbfs"] + r["thdn_db"], 1)}
            inside = bool(w0 >= exp["tone_start"] and w1 <= exp["tone_end"])
            ev["production_replay"] = {"onset": onset,
                                       "onset_s": round(onset / FS, 4),
                                       "window": [w0, w1],
                                       "blocks_full": blocks_full,
                                       "analyzed_frames": used,
                                       "thdn": thd,
                                       "window_inside_tone": inside}

        run_rec = {"cond": cond, "lead_s": lead_s, "trailing_s": trail_s,
                   "fmt": fmt_facts, "observer": observer_meta, "events": ev}
        json.dump(run_rec, open(OUT / f"v25-run-{idx+1}-{cond}.json", "w",
                                encoding="utf-8"), ensure_ascii=False, indent=1,
                  default=str)
        summary.append(run_rec)
        pr = ev["production_replay"]
        print(f"  маркерA: {ev['marker_A_R']['global_abs']} "
              f"({ev['marker_A_R']['score']}) | маркерБ: "
              f"{ev['marker_B_R']['global_abs']} ({ev['marker_B_R']['score']}) "
              f"| Б Δ {ev['marker_B_residual_ms']} мс | onset {pr.get('onset_s')} "
              f"| THD+N {pr.get('thdn', {}).get('L', {}).get('thdn_pct')}/"
              f"{pr.get('thdn', {}).get('R', {}).get('thdn_pct')} %", flush=True)

    json.dump({"order": [str(o) for o in order], "fmt": fmt_facts,
               "summary": summary},
              open(OUT / "v25-summary.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
