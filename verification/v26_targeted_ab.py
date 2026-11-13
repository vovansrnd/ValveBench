#!/usr/bin/env python3
"""v2.6-целевой A/B (§14.5.7): lead=0 против lead=1.5, ОДИНАКОВЫЙ хвост 0.6 с,
после РЕАЛЬНОГО clock preflight (production _seg, 1 кГц −30, 4.45 с),
БЕЗ искусственных пауз. Фактический интервал preflight→THD логируется.

Чередование: L0, L1.5, L1.5, L0, L0, L1.5, L1.5, L0 (8 прогонов, 4 на условие).
Программа прогона: [lead][тон 997 −30 dBFS 4.55 с непрерывно — rig._seg с
хвостом 0.5, включая fade-out; чирпы-маркеры не используются].
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


def main():
    dac, adc = find_card(DAC_USBID), find_card(ADC_USBID)
    assert dac is not None and adc is not None, "D10/E-MU не найдены"
    hw_dac, hw_adc = f"hw:{dac},0", f"hw:{adc},0"
    OUT = ROOT / "measurements" / "diagnostics" / "v26-ab"
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = calib.Calibration()
    be = AlsaCli(hw_out=hw_dac, hw_in=hw_adc,
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    be.open(FS)
    print(f"fmt_out={be.fmt_out} fmt_in={be.fmt_in} buffer_ms={be.buffer_ms}", flush=True)

    rig = m.Rig(SimBackend(), cfg, fs=FS, df=1.0, blocks=4, settle=0.5)
    k1, a1 = rig.amp_tone(1000.0, -30.0)
    preflight, _ = rig._seg([(k1, a1)], [(k1, a1)], 0.4)  # 4.45 с, как в clock_check
    k9, a9 = rig.amp_tone(997.0, -30.0)
    tone_prog, tone_s_n = rig._seg([(k9, a9)], [(k9, a9)], 0.5)  # 4.55 с непрерывный тон

    order = ["L0", "L1.5", "L1.5", "L0", "L0", "L1.5", "L1.5", "L0"]
    results = []
    for idx, cond in enumerate(order):
        lead_s = 1.5 if cond == "L1.5" else 0.0
        lead = np.zeros((int(lead_s * FS), 2))
        program = np.concatenate([lead, tone_prog])   # тон + 0.6 с хвоста (одинаково)

        print(f"\nпрогон {idx+1}/8 ({cond}):", flush=True)
        t_pre0 = time.monotonic()
        be.play_record(preflight.copy())
        t_pre1 = time.monotonic()
        preflight_dur = round(t_pre1 - t_pre0, 3)

        t_gap = time.monotonic()
        with SubprocessObserverCtx(OUT, tag=f"r{idx+1}"):
            t_thd0 = time.monotonic()
            rec = be.play_record(program.copy())
            t_thd1 = time.monotonic()
        interval = round(t_thd0 - t_pre1, 3)

        capL, capR = rec[:, 0].astype(np.float64), rec[:, 1].astype(np.float64)
        n_cap = len(rec)
        src = src_frames(4.55, lead_s, 0.6)
        ev = {}
        for name, arr in (("L", capL), ("R", capR)):
            xa = xcorr_markers(arr, chirp(FS, 0.08, 6500, 7500))
            ev[f"chirpA_{name}"] = {"global_abs": xa["global"][0],
                                    "score": round(xa["global"][1], 4),
                                    "n_maxima": len(xa["maxima"])}
        # плато тона: огибающая 997, −45 дБФС, устойчивость 30 мс
        env = demod_env(capR, FS, 997)
        above = np.where(env > 10 ** (-45 / 20))[0]
        run_min = int(0.03 * FS)
        pstart = pend = None
        if len(above):
            for i in above:
                if (env[i:i + run_min] > 10 ** (-45 / 20)).all():
                    pstart = i; break
            for i in above[::-1]:
                if (env[max(0, i - run_min + 1):i + 1] > 10 ** (-45 / 20)).all():
                    pend = i + 1; break
        plateau_len = (pend - pstart) / FS if (pstart is not None and pend is not None) else None
        # ожидаемый конец тона: плато-старт + 4.54 (4.55 − fade-in) — при lead=0
        # обрезанный тон кончается раньше; при lead=1.5 — на месте
        # корректный расчёт: плато-старт = СТАРТ ТОНА (env997 поднимается на тоне;
        # лидирующая тишина тиха и в плато не входит) →
        # ожидаемый конец тона = плато-старт + 4.54 с (4.55 − fade-in);
        # lead_s в формулу НЕ входит (тишина рендерится до тона, не сдвигает его)
        # поданный конец тона: старт плато (подъём env997 = старт тона после
        # fade-in) + 4.54 с (4.55 − fade-in 0.01) — методика v2.6. Без этого
        # определения имя было не определено (дефект §14.5.5 — исправлен)
        submitted_tone_end = (pstart + int(round(4.54 * FS))
                              if pstart is not None else None)
        last45 = None
        if pstart is not None:
            above2 = np.where(env[pstart:pend] > 10 ** (-45 / 20))[0]
            if len(above2):
                last45 = int(above2[-1] + pstart)
        tone_loss_s = (round((submitted_tone_end - last45) / FS, 4)
                       if (last45 is not None and submitted_tone_end is not None)
                       else None)

        r = {"run": idx + 1, "cond": cond, "lead_s": lead_s,
             "preflight_duration_s": preflight_dur,
             "preflight_to_thd_interval_s": interval,
             "captured_s": round(n_cap / FS, 3),
             "plateau_start_s": round(pstart / FS, 4) if pstart is not None else None,
             "plateau_len_s": round(plateau_len, 4) if plateau_len else None,
             "submitted_tone_end_s": round(submitted_tone_end / FS, 4)
             if submitted_tone_end is not None else None,
             "tone_last_above45_s": round(last45 / FS, 4) if last45 else None,
             "tone_loss_s": tone_loss_s,
             "chirpA": {k2: ev[f"chirpA_{k2}"] for k2 in ("L", "R")},
             "rec_frames": n_cap}
        results.append(r)
        print(f"  плато {plateau_len if plateau_len else '—'} с | потеря тона "
              f"{tone_loss_s} с | preflight {preflight_dur} с | интервал до THD {interval} с",
              flush=True)
        wavfile.write(OUT / f"v26ab-capture-{idx+1}-{cond}.wav", FS,
                      rec.astype(np.float32))
        wavfile.write(OUT / f"v26ab-program-{idx+1}-{cond}.wav", FS,
                      program.astype(np.float32))
        time.sleep(0.5)

    print("\n=== СВОДКА ===")
    for r in results:
        print(f"{r['cond']:5s}: плато {r['plateau_len_s']} с | потеря тона "
              f"{r['tone_loss_s']} с | preflight→THD {r['preflight_to_thd_interval_s']} с")
    losses = [r["tone_loss_s"] for r in results if r["tone_loss_s"] is not None]
    l0 = [r["tone_loss_s"] for r in results if r["cond"] == "L0" and r["tone_loss_s"] is not None]
    l15 = [r["tone_loss_s"] for r in results if r["cond"] == "L1.5" and r["tone_loss_s"] is not None]
    print(f"\nпотеря L0: {l0} | потеря L1.5: {l15}")
    json.dump({"fs": FS, "order": order, "results": results},
              open(OUT / "v26ab-summary.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("JSON:", OUT / "v26ab-summary.json")


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


if __name__ == "__main__":
    main()
