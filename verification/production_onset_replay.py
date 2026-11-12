#!/usr/bin/env python3
"""Офлайн-реплей production onset/THD на сохранённых захватах координатора.
v2.3 (§12.4.6): убран якорь по периодическому синусу (многозначен — §12.4
координатора); истинная зона тона = ПЛАТО из огибающей самого захвата
(2-мс RMS, порог −55 dBFS, устойчивость 50 мс); в JSON — фактически
проанализированные кадры (_blocks-усечение) и число полных блоков.

Production-код не изменяется: Rig._onset / Rig._tone_an вызываются как есть.
"""
import json
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


def plateau_bounds(cap, fs, thr_db=-55.0, win_s=0.002, sustain_s=0.05):
    """Границы устойчивого плато тона по огибающей самого захвата.
    Возвращает (start_frame, end_frame, db_массив_2мс)."""
    w = int(round(win_s * fs))
    n = len(cap) // w * w
    rms = np.sqrt(np.mean(cap[:n].reshape(-1, w, 2) ** 2, axis=1)).max(axis=1) * np.sqrt(2)
    db = 20 * np.log10(np.clip(rms, 1e-12, None))
    hi = db > thr_db
    run_min = int(round(sustain_s / win_s))
    idx = np.where(hi)[0]
    start = end = None
    if len(idx):
        for i in idx:
            if hi[i:i + run_min].all():
                start = i
                break
        for i in idx[::-1]:
            if hi[max(0, i - run_min + 1):i + 1].all():
                end = i + 1
                break
    return (start * w if start is not None else None,
            end * w if end is not None else None, db)


def main():
    be = SimBackend()
    cal = calib.Calibration()
    out = []
    for case in CASES:
        d = DIAG / case
        meta = json.load(open(d / "metadata.json"))
        fs = meta["sample_rate_hz"]
        run = next(r for r in meta["runs"] if r["label"] == "thd")
        _, cap = wavfile.read(d / "capture_full_thd.wav")
        cap = cap.astype(np.float64)
        n_cap = len(cap)

        # истинное плато по огибающей захвата
        p0, p1, db = plateau_bounds(cap, fs)
        onset = run["detected_tone_onset_frame"]
        settle_f = run["settle_frames"]
        rig_n = fs  # df=1.0 → n = fs (подтверждено: окно 4.0 с = blocks 4 × 1.0 с)
        win_len = meta["blocks"] * rig_n
        program_tone_len = run["stimulus_frames"]

        # production окно (как в Rig.run): onset + settle, длина blocks*n
        old_w0 = onset + settle_f
        old_w1 = old_w0 + win_len

        # окно внутри плато: старт = плато+0.4 с, конец ≤ плато_end − 0.1 с
        fit_w0 = p0 + int(0.4 * fs)
        fit_w1 = min(fit_w0 + win_len, p1 - int(0.1 * fs))
        fit_len = int(fit_w1 - fit_w0)
        fit_blocks = int(fit_len // rig_n)
        fit_used = int(fit_blocks * rig_n)

        # production onset vs плато
        onset_vs_plateau_ms = round((onset - p0) / fs * 1000, 1)
        # обрезание: плато_end − (onset + program_tone_len)
        trunc_s = round((p1 - (onset + program_tone_len)) / fs, 4)

        rig = m.Rig(be, cal, fs=fs, df=1.0, blocks=meta["blocks"],
                    settle=meta["settle_seconds"])
        k, _ = rig.amp_tone(meta["test_frequency_hz"], meta["generator_level_dbfs"])
        thd = {}
        for label, w0, w1 in (("production", old_w0, old_w1), ("fitting", fit_w0, fit_w1)):
            w0, w1 = int(w0), int(w1)
            blocks_used = int((w1 - w0) // rig_n)
            used_frames = int(blocks_used * rig_n)
            e = {"window_frames": [w0, w1],
                 "window_len_s": round((w1 - w0) / fs, 4),
                 "blocks_full": blocks_used,
                 "analyzed_frames": used_frames,
                 "analyzed_len_s": round(used_frames / fs, 4),
                 "truncated_by_blocks": bool(used_frames < (w1 - w0))}
            for c, ch in ((0, "L"), (1, "R")):
                r = rig._tone_an(cap[w0:w0 + used_frames], c, k, 9)
                e[f"thdn_pct_{ch}"] = round(r["thdn_pct"], 3)
                e[f"residual_dbfs_{ch}"] = round(r["level_dbfs"] + r["thdn_db"], 1)
            thd[label] = e

        rec = {
            "case": case,
            "plateau_frames": [int(p0), int(p1)],
            "plateau_s": [round(p0 / fs, 4), round(p1 / fs, 4)],
            "plateau_len_s": round((p1 - p0) / fs, 4),
            "production_onset_frame": int(onset),
            "onset_vs_plateau_start_ms": onset_vs_plateau_ms,
            "program_tone_len_s": round(program_tone_len / fs, 4),
            "truncation_s": trunc_s,
            "thd": thd,
        }
        out.append(rec)
        print(f"=== {case}")
        print(f"  плато {p0/fs:.3f}..{p1/fs:.3f} с ({rec['plateau_len_s']} с); "
              f"onset {onset/fs:.3f} с (Δ {onset_vs_plateau_ms:+.1f} мс); "
              f"усечение тона: {trunc_s} с")
        p = thd["production"]; f = thd["fitting"]
        print(f"  production-окно ({p['window_len_s']} с, {p['blocks_full']} полных блоков): "
              f"THD+N L {p['thdn_pct_L']} % / R {p['thdn_pct_R']} %")
        print(f"  окно внутри плато ({f['analyzed_len_s']} с = {f['blocks_full']} полных "
              f"блоков, усечено длиной плато: {f['truncated_by_blocks']}): "
              f"THD+N L {f['thdn_pct_L']} % / R {f['thdn_pct_R']} %", flush=True)

    path = DIAG / "production-onset-replay-v23.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("JSON:", path)


if __name__ == "__main__":
    main()
