#!/usr/bin/env python3
"""Офлайн-реплей production onset-детектора и THD-анализа на СОХРАНЁННЫХ
захватах координатора (задание §12.4 / финальное сообщение: «прямое
доказательство или опровержение основной гипотезы»).

Production-код НЕ изменяется: используются valvebench.measure.Rig._onset
и Rig._tone_an как есть (Rig создаётся на SimBackend — железо не трогается).

Для каждого диагностического архива:
  1. захват и stimulus (точно поданная программа) из metadata.json;
  2. истинная позиция тона в захвате: полная кросс-корреляция stimulus→capture
     (стимул содержит фейды и для run D — postroll, что делает пик однозначным);
  3. production onset: Rig._onset(capture, tone_hz) — как в thd_point;
  4. production окно: onset + settle_frames, длина blocks*n — как в Rig.run;
  5. THD+N production-анализом (Rig._tone_an) на СТАРОМ окне и на
     ВЫРОВНЕННОМ окне (start = истинный старт тона + settle);
  6. RMS-профиль по 0.5 с: где тон, где окно, где остаток.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from valvebench import measure as m, calib  # noqa: E402
from valvebench.backend import SimBackend  # noqa: E402

DIAG = ROOT / "measurements" / "diagnostics"
CASES = ["d10-emu-thd-full-capture", "d10-emu-thd-full-capture-4blocks",
         "d10-emu-thd-postroll-06", "d10-emu-thd-preroll-06",
         "d10-emu-thd-tone-tail-06"]


def fft_xcorr_peak(capture, template):
    n = len(capture); m = len(template)
    L = int(2 ** np.ceil(np.log2(n + m)))
    F = np.fft.rfft(capture, L) * np.conj(np.fft.rfft(template, L))
    cc = np.fft.irfft(F, L)[: n - m + 1]
    energy = np.convolve(capture ** 2, np.ones(m), "valid").clip(1e-30)
    cc = cc[: len(energy)] / (np.linalg.norm(template) * np.sqrt(energy))
    return int(np.argmax(cc)), float(cc.max())


def main():
    be = SimBackend()          # заглушка: железо не открывается
    cal = calib.Calibration()
    out = []
    for case in CASES:
        d = DIAG / case
        meta = json.load(open(d / "metadata.json"))
        fs = meta["sample_rate_hz"]
        run = next(r for r in meta["runs"] if r["label"] == "thd")
        fs_cap, cap = wavfile.read(d / "capture_full_thd.wav")
        _, stim = wavfile.read(d / "stimulus_thd.wav")
        cap = cap.astype(np.float64)
        stim = stim.astype(np.float64)
        n_cap = len(cap)

        # 1) истинная задержка: полная кросс-корреляция stimulus→capture
        chunk = stim[int(0.3 * fs):int(0.8 * fs), 0]
        lat_frames, lat_score = fft_xcorr_peak(cap[:, 0], chunk)
        # позиция куска в стимуле: 0.3 с
        stim0_in_capture = lat_frames - int(0.3 * fs)
        pad_frames = 9600  # 0.05 с, добавляется AlsaCli.play_record
        stim0_true = stim0_in_capture - pad_frames   # кадр стимула 0 в базе захвата

        # 2) истинная зона тона в захвате: весь stimulus — тон (по _seg тон с
        #    сэмпла 0), кроме tone-tail (там stimulus = тон + тишина 0.6 с)
        tone_frames = run.get("source_frames_before_tone_postroll",
                              run["stimulus_frames"])
        true_tone_start = stim0_true
        true_tone_end = stim0_true + tone_frames

        # 3) production onset — БЕЗ ИЗМЕНЕНИЙ
        rig = m.Rig(be, cal, fs=fs, df=1.0, blocks=meta["blocks"],
                    settle=meta["settle_seconds"])
        try:
            onset = rig._onset(cap, tone_hz=meta["test_frequency_hz"])
            onset_err = "—"
        except Exception as exc:
            onset, onset_err = None, f"{type(exc).__name__}: {str(exc)[:60]}"

        # 4) production окно
        settle_f = run["settle_frames"]
        win_len = meta["blocks"] * rig.n
        old_w0 = onset + settle_f if onset is not None else None
        old_w1 = old_w0 + win_len if old_w0 is not None else None

        # 5) выровненное окно: старт тона + settle, длина та же
        al_w0 = true_tone_start + settle_f
        al_w1 = al_w0 + win_len

        def rms(x):
            return 20 * np.log10(max(float(np.sqrt(np.mean(x ** 2)) * np.sqrt(2)), 1e-12))

        # 6) THD+N production-анализом на обоих окнах
        k, _ = rig.amp_tone(meta["test_frequency_hz"], meta["generator_level_dbfs"])
        thd = {}
        for label, w0, w1 in (("old", old_w0, old_w1), ("aligned", al_w0, al_w1)):
            entry = {"window": [w0, w1] if w0 is not None else None}
            if w0 is not None and 0 <= w0 and w1 <= n_cap:
                for c, ch in ((0, "L"), (1, "R")):
                    r = rig._tone_an(cap[w0:w1], c, k, 9)
                    entry[f"thdn_pct_{ch}"] = r["thdn_pct"]
                    entry[f"residual_dbfs_{ch}"] = round(
                        r["level_dbfs"] + r["thdn_db"], 1)
                    entry[f"level_dbfs_{ch}"] = round(r["level_dbfs"], 2)
            else:
                entry["error"] = "окно вне захвата"
            thd[label] = entry

        # 7) RMS-профиль 0.5-с окон захвата
        profile = []
        for i in range(0, n_cap - fs // 2, fs // 2):
            seg = cap[i:i + fs // 2]
            profile.append(round(20 * np.log10(max(
                float(np.sqrt(np.mean(seg ** 2)) * np.sqrt(2)), 1e-12)), 1))

        rec = {
            "case": case,
            "stimulus_s": round(run["stimulus_seconds"], 3),
            "tone_frames_in_stimulus": tone_frames,
            "stim0_true_capture_frame": stim0_true,
            "latency_score": round(lat_score, 3),
            "true_tone_capture": [true_tone_start, true_tone_end],
            "true_tone_start_s": round(true_tone_start / fs, 4),
            "production_onset_frame": onset,
            "production_onset_s": round(onset / fs, 4) if onset is not None else onset_err,
            "onset_minus_true_tone_s": round((onset - true_tone_start) / fs, 4) if onset is not None else onset_err,
            "old_window": [old_w0, old_w1],
            "aligned_window": [al_w0, al_w1],
            "old_window_covers_tone": (old_w0 is not None and old_w0 >= true_tone_start
                                       and old_w1 <= true_tone_end),
            "thd": thd,
            "rms_profile_0_5s": profile,
            "metadata_onset_frame": run["detected_tone_onset_frame"],
        }
        out.append(rec)
        print(f"=== {case}")
        print(f"  стимул {run['stimulus_seconds']} с, тон {tone_frames} кадров; "
              f"истинный тон в захвате: {true_tone_start}..{true_tone_end} "
              f"(t={true_tone_start/fs:.3f}..{true_tone_end/fs:.3f} с), corr={lat_score:.3f}")
        on = rec["production_onset_frame"]
        if on is not None:
            print(f"  production onset: {on} (t={on/fs:.3f} с) — смещение от истинного "
                  f"старта тона: {(on-true_tone_start)/fs:+.3f} с")
        else:
            print(f"  production onset: {onset_err}")
        print(f"  окно production: {old_w0}..{old_w1}; целиком внутри тона: "
              f"{rec['old_window_covers_tone']}")
        for lbl in ("old", "aligned"):
            t = thd[lbl]
            if "error" in t:
                print(f"  THD+N [{lbl}]: {t['error']}")
            else:
                print(f"  THD+N [{lbl}]: L {t['thdn_pct_L']:.3f}% (ост {t['residual_dbfs_L']}) | "
                      f"R {t['thdn_pct_R']:.3f}% (ост {t['residual_dbfs_R']})")
        print(f"  RMS-профиль (0.5 с): {rec['rms_profile_0_5s']}", flush=True)

    out_path = DIAG / "production-onset-replay.json"
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("JSON:", out_path)


if __name__ == "__main__":
    main()
