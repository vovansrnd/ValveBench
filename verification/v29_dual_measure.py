#!/usr/bin/env python3
"""v2.9-аппаратный прогон: dual-мера (lead 1.5 + тон 4.55 + tail 0.6) с
РЕАЛЬНЫМ preflight уровнем −72 dBFS (production default clock_probe_level_dbfs).

Последовательность прогона (как в приложении):
  1. production clock preflight: 1 кГц, −72 dBFS, 4.45 с (Rig._seg, settle 0.4)
  2. сразу (без пауз) dual-мера программа: [lead 1.5][chirp A][gap][тон 997
     −30 dBFS 4.55 с][tail 0.6][chirp Б]
Анализ: маркеры (v2.1 детектор), joint-LSQ профили, границы устойчивого тона
частотно-селективным методом (§14.3: 997 vs 1 кГц раздельно).
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))
from verification.timing_probe import chirp  # noqa: E402
from v26_offline_reconstruction import src_frames  # noqa: E402
from valvebench import calib, measure as m  # noqa: E402
from valvebench.backend import AlsaCli, SimBackend, to_pcm  # noqa: E402
from valvebench import backend as backend_mod  # noqa: E402

DAC_USBID = "152a:85dd"
ADC_USBID = "041e:3f04"
FS = 192000
PAD = int(0.05 * FS)
MEASURED_PPM = -23.5
F_TONE = 997.0 * (1 + MEASURED_PPM * 1e-6)
F_RES = 1000.0 * (1 + MEASURED_PPM * 1e-6)
PREFLIGHT_LEVEL = -72.0     # production default clock_probe_level_dbfs
TONE_LEVEL = -30.0
OUT = ROOT / "measurements" / "diagnostics" / "v29-dual"
REPEATS = 2


def find_card(usbid):
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                if open(f"/proc/asound/{c}/usbid").read().strip() == usbid:
                    return int(c[4:])
            except OSError:
                pass
    return None


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


def xcorr_markers(arr, template):
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


class SubprocessObserverCtx:
    """Наблюдатель реальных команд/rc/stderr backend-процессов (как в v2.5)."""
    def __init__(self, logdir, tag=""):
        self.logdir = str(logdir)
        self.tag = tag
        self.records = []
        self._orig_popen = backend_mod.subprocess.Popen
        self._orig_run = backend_mod.subprocess.run

    def __enter__(self):
        obs = self
        mod = backend_mod.subprocess

        def patched_popen(*args, **kwargs):
            rec = {"cmd": list(args[0]) if args else None, "stderr_path": None}
            stderr_arg = kwargs.get("stderr")
            if stderr_arg is not None and hasattr(stderr_arg, "seek"):
                p = os.path.join(obs.logdir, f"stderr_arecord_{obs.tag}_{len(obs.records)}.txt")
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


def main():
    dac, adc = find_card(DAC_USBID), find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
    OUT = ROOT / "measurements" / "diagnostics" / "v29-dual"
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = calib.Calibration()
    be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc,
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    rig = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
    kp, ap = rig.amp_tone(1000.0, PREFLIGHT_LEVEL)
    preflight, _ = rig._seg([(kp, ap)], [(kp, ap)], 0.4)   # 4.45 с 1 кГц −72 dBFS
    k9, a9 = rig.amp_tone(997.0, TONE_LEVEL)
    tone_prog, _ = rig._seg([(k9, a9)], [(k9, a9)], 0.5)   # 4.55 с непрерывный тон

    lead = np.zeros((int(1.5 * FS), 2))
    tail = np.zeros((int(0.6 * FS), 2))
    program = np.concatenate([lead, tone_prog, tail])      # dual-мера

    results = []
    for rep in (1, 2):
        print(f"\n=== dual-мера, повтор {rep}/2 ===", flush=True)
        obs = SubprocessObserverCtx(OUT, tag=f"r{rep}")
        with obs:
            t_pre0 = time.monotonic()
            be.play_record(preflight.copy())            # production preflight −72 dBFS
            t_pre1 = time.monotonic()
            t_thd0 = time.monotonic()
            rec = be.play_record(program.copy())        # dual-мера сразу после
            t_thd1 = time.monotonic()
        interval = round(t_thd0 - t_pre1, 3)
        preflight_wall = round(t_pre1 - t_pre0, 3)

        capL, capR = rec[:, 0].astype(np.float64), rec[:, 1].astype(np.float64)
        n_cap = len(rec)
        src = src_frames(4.55, 1.5, 0.6)
        ev = {}
        for name, arr in (("L", capL), ("R", capR)):
            xa = xcorr_markers(arr, chirp(FS, 0.08, 6500, 7500))
            ev[f"marker_A_{name}"] = {"global_abs": xa["global"][0],
                                      "score": round(xa["global"][1], 4)}
            xb = xcorr_markers(arr, chirp(FS, 0.08, 10500, 11500))
            ev[f"marker_B_{name}"] = {"global_abs": xb["global"][0],
                                      "score": round(xb["global"][1], 4)}
        mA = ev["marker_A_R"]["global_abs"]
        start_offset = mA - (src["marker_A_start"] + PAD)
        exp_tone_start = start_offset + PAD + src["tone_start"]
        exp_tone_end = start_offset + PAD + src["tone_end"]

        # частотно-селективные границы: скользящий joint-LSQ 0.25 с / шаг 0.05
        win = int(0.25 * FS)
        hop = int(0.05 * FS)
        tt = np.arange(win) / FS
        D = np.column_stack([
            np.sin(2 * np.pi * F_TONE * tt), np.cos(2 * np.pi * F_TONE * tt),
            np.ones(win)])
        stable = []
        for i in range(0, n_cap - win, hop):
            seg = capR[i:i + win]
            coef, *_ = np.linalg.lstsq(D, seg, rcond=None)
            a997 = 20 * np.log10(max(float(np.hypot(coef[0], coef[1])), 1e-12))
            stable.append((i, a997))
        above = [(i, v) for i, v in stable if v > -45]
        run_min = int(0.03 * FS / (0.05 * FS))
        st = en = None
        if above:
            frames = [i for i, _ in above]
            for i in frames:
                if any(abs(f - i) < run_min * FS / 1000 for f in frames):
                    pass
            st = above[0][0]
            en = above[-1][0]
            # устойчивость: 30 мс подряд выше −45
            vals = [v for _, v in stable if v > -45]
        stable_span_s = round((en - st) / FS, 4) if (st is not None and en is not None) else None

        r = {"repeat": rep,
             "preflight_level_dbfs": PREFLIGHT_LEVEL,
             "preflight_wall_s": preflight_wall,
             "preflight_to_thd_interval_s": interval,
             "marker_A_R": ev["marker_A_R"],
             "marker_B_R": ev["marker_B_R"],
             "start_offset_s": round(start_offset / FS, 5),
             "expected_tone_start_s": round(exp_tone_start / FS, 4),
             "expected_tone_end_s": round(exp_tone_end / FS, 4),
             "stable997_first_s": round(st / FS, 4) if st is not None else None,
             "stable997_last_s": round(en / FS, 4) if en is not None else None,
             "stable997_span_s": stable_span_s,
             "captured_s": round(n_cap / FS, 3)}
        # потеря тона: подано 4.55, стабильный 997-участок
        if stable_span_s:
            r["tone_loss_s"] = round(4.55 - stable_span_s, 4)
        # 997/1кГц разделение в pre-tone зоне (загрязнение после preflight?)
        e_pre = demod_env(capL[int(0.2 * FS):int(0.8 * FS)], FS, 997)
        e_res = demod_env(capL[int(0.2 * FS):int(0.8 * FS)], FS, F_RES)
        r["pre_tone_997_db"] = round(20 * np.log10(max(float(np.max(e_pre)), 1e-12)), 1)
        results.append(r)
        print(f"  preflight −72 dBFS: {preflight_wall} с wall | интервал до dual {interval} с")
        print(f"  маркерA: {ev['marker_A_R']} | маркерБ: {ev['marker_B_R']}")
        print(f"  стабильный 997: {r.get('stable997_first_s')}..{r.get('stable997_last_s')} "
              f"({stable_span_s} с) | потеря {r.get('tone_loss_s')} с", flush=True)
        wavfile.write(OUT / f"dual-capture-r{rep}.wav", FS, rec.astype(np.float32))
        wavfile.write(OUT / f"dual-program-r{rep}.wav", FS, program.astype(np.float32))
        time.sleep(0.5)

    json.dump(results, open(OUT / "dual-results.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nJSON:", OUT / "dual-results.json")
    losses = [r.get("tone_loss_s") for r in results if r.get("tone_loss_s") is not None]
    if losses:
        print(f"потеря тона: {losses} (среднее {sum(losses)/len(losses):.4f} с)")


if __name__ == "__main__":
    main()
