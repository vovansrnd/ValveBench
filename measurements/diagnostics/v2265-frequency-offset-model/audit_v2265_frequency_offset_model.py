#!/usr/bin/env python3
"""v2.26.5 offline pure-tone frequency-offset null model (D10 -> E-MU 0404).

Part A reuses the fail-closed v2264 window recovery. Part B builds synthetic
pure-tone models for all 324 measurement windows and tests whether the
commanded-vs-measured frequency offset alone can explain unstable_carrier.
No ALSA/backend/device access. Run from repo root:
PYTHONPATH=. python3 <script> --out-dir <dir>
"""
import argparse, csv, hashlib, json, math, os, sys
import numpy as np

from valvebench.gainmatch import envelope_stability
from valvebench.measure import Rig
from valvebench.backend import SimBackend, SimChannel
from valvebench.calib import Calibration

HEAD_SHA = "357be1f3ca508e213e0ab59f3ee7f7afe76a36f5"
FS, DF, BLOCKS, SETTLE = 192000, 1.0, 2, 0.5
FREQS = (94.0, 994.0, 9994.0)
REPEATS = 3
SEGN = int(round(SETTLE * FS)) + BLOCKS * int(round(FS / DF)) + int(0.05 * FS)
SETTLE_N = int(round(SETTLE * FS))
WIN_N = BLOCKS * int(round(FS / DF))
JSON_READS = []

RUNS = {
    "A_v2261r1": "measurements/diagnostics/v2261-gainmatch-level-margin/results/vovans-PC-run-01",
    "B_v2262run1": "measurements/diagnostics/v2262-gainmatch-repeatability/results/vovans-PC-run-01",
    "C_v2263run2": "measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-02",
    "D_v2263run3": "measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-03",
}
rig = Rig(SimBackend(SimChannel(gain=0.5, a2=1e-5, a3=1e-5, riaa=False, noise_dbfs=-140),
                     SimChannel(gain=0.5, a2=1e-5, a3=1e-5, riaa=False, noise_dbfs=-140),
                     seed=1), Calibration(), fs=FS, df=DF, blocks=BLOCKS, settle=SETTLE)


def fail(out, msg):
    out["errors"].append(msg)
    raise RuntimeError(msg)


def order_of(i):
    return FREQS[i % 3], i // 3 + 1


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fit_fundamental(sig, f0, fs=FS):
    """LS fit s(t)=c0+c1*cos(w t)+c2*sin(w t); amp=hypot(c1,c2), phase=atan2(c2,c1),
    signal model s(t)=amp*cos(2*pi*f0*t - phase). Returns (amp, phase, c0, resid)."""
    t = np.arange(sig.size) / float(fs)
    w = 2.0 * math.pi * f0
    basis = np.column_stack((np.ones(t.size), np.cos(w * t), np.sin(w * t)))
    coef = np.linalg.lstsq(basis, sig, rcond=None)[0]
    amp = math.hypot(coef[1], coef[2])
    phase = math.atan2(coef[2], coef[1])
    model = coef[0] + coef[1] * np.cos(w * t) + coef[2] * np.sin(w * t)
    return amp, phase, float(coef[0]), sig - model


def synth(amp, phase, f0, n=WIN_N, fs=FS, phase_override=None):
    t = np.arange(n) / float(fs)
    ph = phase if phase_override is None else phase_override
    return amp * np.cos(2.0 * math.pi * f0 * t - ph)


def recover_windows(npz_path, out):
    z = np.load(npz_path)
    for key in ("submitted_to_backend", "capture", "fs", "lead_seconds"):
        if key not in z.files:
            fail(out, "npz %s missing key %s" % (npz_path, key))
    capture = np.asarray(z["capture"], dtype=np.float64)
    submitted = np.asarray(z["submitted_to_backend"], dtype=np.float64)
    if capture.ndim != 2 or capture.shape[1] != 2 or submitted.ndim != 2 or submitted.shape[1] != 2:
        fail(out, "npz %s bad shape" % npz_path)
    if not (np.isfinite(capture).all() and np.isfinite(submitted).all()):
        fail(out, "npz %s NaN/Inf" % npz_path)
    if int(z["fs"]) != FS:
        fail(out, "npz %s fs != %d" % (npz_path, FS))
    lead_n = int(round(float(z["lead_seconds"]) * FS))
    d = rig._onset(capture, tone_hz=FREQS[0])
    wins = []
    for i in range(REPEATS * len(FREQS)):
        a = d + i * SEGN + SETTLE_N
        w = capture[a:a + WIN_N, :]
        if w.shape != (WIN_N, 2) or not np.isfinite(w).all():
            fail(out, "npz %s window %d bad" % (npz_path, i))
        wins.append(w)
    return dict(onset=d, lead_n=lead_n, wins=wins)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    out = dict(errors=[], rows=[], events={}, highz={})

    npz_manifest = []
    captures = {}
    for tag, run in RUNS.items():
        sj = os.path.join(run, "SHA256SUMS.json")
        JSON_READS.append(sj)
        JSON_READS.append(os.path.join(run, "clock-preflight.json"))
        JSON_READS.append(os.path.join(run, "run-summary.json"))
        sums = json.load(open(sj))
        for fp, meta in sums["files"].items():
            if not fp.endswith(".npz"):
                continue
            full = os.path.join(run, fp)
            ok = (sha256_of(full) == meta["sha256"] and os.path.getsize(full) == meta["size_bytes"])
            npz_manifest.append(dict(run=tag, file=fp, sha256_ok=ok, size_bytes=meta["size_bytes"]))
            if not ok:
                fail(out, "SHA mismatch %s/%s" % (tag, fp))
        for fp in sorted(f for f in sums["files"] if f.endswith(".npz") and "preflight" not in f):
            base = os.path.basename(fp)
            level = "m" + base.split("-")[0][1:]
            sname = ("loopback" if "loopback-none" in base else
                     "inject-R" if "inject_R" in base else "inject-L")
            sjp = os.path.join(run, "level_%s" % level, "%s-series.json" % sname)
            JSON_READS.append(sjp)
            series = json.load(open(sjp))
            w = recover_windows(os.path.join(run, fp), out)
            captures[(tag, base)] = w
            for i in range(REPEATS * len(FREQS)):
                freq, rep = order_of(i)
                k = int(round(freq * DF))
                f_cmd = rig.f_of(k)
                for ci, cname in ((0, "L"), (1, "R")):
                    win = w["wins"][i]
                    sig = win[:, ci]
                    an = rig._tone_an(win, ci, k, nharm=9)
                    f_meas = float(an["f0"])
                    A_m, ph_m, _, resid = fit_fundamental(sig, f_meas)
                    A_c, ph_c, _, _ = fit_fundamental(sig, f_cmd)
                    syn_m = synth(A_m, ph_m, f_meas)
                    syn_c = synth(A_c, ph_c, f_cmd)
                    z_ac = envelope_stability(sig, FS, f_cmd)
                    z_am = envelope_stability(sig, FS, f_meas)
                    z_sc = envelope_stability(syn_m, FS, f_cmd)
                    z_sm = envelope_stability(syn_m, FS, f_meas)
                    z_cc = envelope_stability(syn_c, FS, f_cmd)
                    jr = None
                    for r in series["rows"]:
                        if abs(r["requested_frequency_hz"] - freq) < 1e-6:
                            for rp in r["channels"][cname]["repeats"]:
                                if rp.get("repeat") == rep:
                                    jr = rp
                    out["rows"].append(dict(
                        run=tag, npz=base, level=level, series=sname, seg=i,
                        freq=freq, rep=rep, chan=cname,
                        f_cmd=f_cmd, f_meas=f_meas, offset_hz=f_meas - f_cmd,
                        offset_ppm=(f_meas / f_cmd - 1.0) * 1e6,
                        z_actual_cmd=z_ac["z_burst"], z_actual_meas=z_am["z_burst"],
                        z_synmeas_cmd=z_sc["z_burst"], z_synmeas_meas=z_sm["z_burst"],
                        z_syncmd_cmd=z_cc["z_burst"],
                        trend_actual_cmd=z_ac["trend_span"], trend_synmeas_cmd=z_sc["trend_span"],
                        amp_meas=A_m, phase_meas=ph_m, amp_cmd=A_c,
                        json_z=(jr or {}).get("envelope", {}).get("z_burst"),
                        carrier_residual_db=(jr or {}).get("carrier_residual_db"),
                        sinad_db=(jr or {}).get("sinad_db"),
                        noise_dbfs=(jr or {}).get("noise_dbfs"),
                        peak_dbfs=(jr or {}).get("peak_dbfs"),
                        valid=(jr or {}).get("valid")))
            print("[%s/%s] done" % (tag, base), flush=True)

    rows = out["rows"]
    if len(rows) != 324:
        fail(out, "row count %d != 324" % len(rows))
    mism = [r for r in rows if r["json_z"] is None or abs(r["z_actual_cmd"] - r["json_z"]) > 1e-6]
    if mism:
        fail(out, "%d z mismatches" % len(mism))
    high = [r for r in rows if r["z_actual_cmd"] >= 4.0]
    if len(high) != 6:
        fail(out, "z>=4 count %d != 6" % len(high))

    # B3: phase and segmentation-shift sensitivity for the 6 high-z windows
    def win_key(r):
        return (r["run"], r["npz"])
    sens = []
    for r in high:
        w = captures[win_key(r)]
        win = w["wins"][r["seg"]]
        sig = win[:, 0 if r["chan"] == "L" else 1]
        k = int(round(r["freq"] * DF))
        f_cmd = rig.f_of(k)
        an = rig._tone_an(win, 0 if r["chan"] == "L" else 1, k, nharm=9)
        f_meas = float(an["f0"])
        A_m, ph_m, _, _ = fit_fundamental(sig, f_meas)
        zs = []
        for j in range(16):
            ph = 2.0 * math.pi * j / 16.0
            zs.append(envelope_stability(synth(A_m, ph_m, f_meas, phase_override=ph), FS, f_cmd)["z_burst"])
            sens.append(dict(kind="phase", run=r["run"], freq=r["freq"], rep=r["rep"], chan=r["chan"],
                             phase_deg=round(math.degrees(ph), 2), z=zs[-1]))
        frac_ge5 = sum(1 for z in zs if z >= 5.0) / 16.0
        shift_zs = []
        t_full = np.arange(WIN_N + 19200) / float(FS)
        base = A_m * np.cos(2.0 * math.pi * f_meas * t_full - ph_m)
        for sh in (0, 4800, 9600, 14400):
            seg = base[sh:sh + WIN_N]
            z = envelope_stability(seg, FS, f_cmd)["z_burst"]
            shift_zs.append(z)
            sens.append(dict(kind="segshift_ms", run=r["run"], freq=r["freq"], rep=r["rep"], chan=r["chan"],
                             phase_deg=sh / FS * 1000.0, z=z))
        out["highz"][str((r["run"], r["npz"], r["freq"], r["rep"], r["chan"]))] = dict(
            phase_min=min(zs), phase_median=float(np.median(zs)), phase_max=max(zs),
            frac_phase_ge5=frac_ge5, segshift_zs=shift_zs)
        print("[SENS] %s %s %.0fHz %s rep%d: phase z min/med/max=%.3f/%.3f/%.3f frac>=5=%.2f | segshift z=%s" % (
            r["run"], r["series"], r["freq"], r["chan"], r["rep"], min(zs), float(np.median(zs)), max(zs),
            frac_ge5, [round(z, 3) for z in shift_zs]), flush=True)

    # B4: residual decomposition for the 6 high-z windows
    res_rows = []
    for r in high:
        w = captures[win_key(r)]
        win = w["wins"][r["seg"]]
        sig = win[:, 0 if r["chan"] == "L" else 1]
        k = int(round(r["freq"] * DF))
        f_cmd = rig.f_of(k)
        an = rig._tone_an(win, 0 if r["chan"] == "L" else 1, k, nharm=9)
        f_meas = float(an["f0"])
        A_m, ph_m, c0, resid = fit_fundamental(sig, f_meas)
        A_c, ph_c, _, _ = fit_fundamental(sig, f_cmd)
        syn_fixed = synth(A_c, ph_c, f_cmd)
        ctrl = syn_fixed + resid
        z_actual = envelope_stability(sig, FS, f_cmd)["z_burst"]
        z_pure = envelope_stability(synth(A_m, ph_m, f_meas), FS, f_cmd)["z_burst"]
        z_ctrl = envelope_stability(ctrl, FS, f_cmd)["z_burst"]
        res_rows.append(dict(run=r["run"], series=r["series"], freq=r["freq"], rep=r["rep"],
                             chan=r["chan"], f_meas=f_meas, offset_hz=f_meas - f_cmd,
                             amp_fund=A_m, rms_resid=float(np.sqrt(np.mean(resid ** 2))),
                             rms_resid_rel=math.sqrt(np.mean(resid ** 2)) / A_m,
                             max_resid_rel=float(np.max(np.abs(resid))) / A_m,
                             z_actual=z_actual, z_pure_measured=z_pure, z_resid_added=z_ctrl))
        print("[RESID] %s %s %.0fHz %s rep%d: off=%+.4fHz rms_resid_rel=%.4f%% max=%.3f%% z: actual=%.3f pure=%.3f resid_added=%.3f" % (
            r["run"], r["series"], r["freq"], r["chan"], r["rep"], f_meas - f_cmd,
            100 * res_rows[-1]["rms_resid_rel"], 100 * res_rows[-1]["max_resid_rel"],
            z_actual, z_pure, z_ctrl), flush=True)

    # INPUT-SHA256SUMS.json: NPZ + JSON
    seen = {}
    for jp in sorted(set(JSON_READS)):
        seen[jp] = dict(kind="json", size_bytes=os.path.getsize(jp), sha256=sha256_of(jp))
    for m in npz_manifest:
        fp = os.path.join(RUNS[m["run"]], m["file"])
        seen[m["file"]] = dict(kind="npz", run=m["run"], size_bytes=os.path.getsize(fp), sha256=sha256_of(fp))
    json.dump(dict(pinned_commit=HEAD_SHA, files=seen), open(os.path.join(args.out_dir, "INPUT-SHA256SUMS.json"), "w"), indent=1)

    json.dump(dict(rows=out["rows"], highz=out["highz"], residuals=res_rows),
              open(os.path.join(args.out_dir, "frequency-offset-model.json"), "w"), indent=1, default=float)

    with open(os.path.join(args.out_dir, "FREQUENCY-OFFSET-MATRIX.csv"), "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["run", "npz", "level", "series", "seg", "freq", "rep", "chan",
                     "f_cmd", "f_meas", "offset_hz", "offset_ppm",
                     "z_actual_cmd", "z_actual_meas", "z_synmeas_cmd", "z_synmeas_meas", "z_syncmd_cmd",
                     "trend_actual_cmd", "trend_synmeas_cmd", "amp_meas", "phase_meas",
                     "json_z", "carrier_residual_db", "sinad_db", "noise_dbfs", "peak_dbfs", "valid"])
        for r in rows:
            wr.writerow([r["run"], r["npz"], r["level"], r["series"], r["seg"], "%.0f" % r["freq"],
                         r["rep"], r["chan"], "%.4f" % r["f_cmd"], "%.6f" % r["f_meas"],
                         "%.6f" % r["offset_hz"], "%.3f" % r["offset_ppm"],
                         "%.6f" % r["z_actual_cmd"], "%.6f" % r["z_actual_meas"],
                         "%.6f" % r["z_synmeas_cmd"], "%.6f" % r["z_synmeas_meas"],
                         "%.6f" % r["z_syncmd_cmd"],
                         "%.8f" % r["trend_actual_cmd"], "%.8f" % r["trend_synmeas_cmd"],
                         "%.8e" % r["amp_meas"], "%.6f" % r["phase_meas"],
                         r["json_z"], r["carrier_residual_db"], r["sinad_db"],
                         r["noise_dbfs"], r["peak_dbfs"], r["valid"]])
    with open(os.path.join(args.out_dir, "PURE-TONE-PHASE-SENSITIVITY.csv"), "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["kind", "run", "freq", "rep", "chan", "phase_deg_or_shift_ms", "z_burst"])
        for s in sens:
            wr.writerow([s["kind"], s["run"], "%.0f" % s["freq"], s["rep"], s["chan"],
                         s["phase_deg"], "%.6f" % s["z"]])
    with open(os.path.join(args.out_dir, "EVENT-RESIDUALS.csv"), "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["run", "series", "freq", "rep", "chan", "f_meas", "offset_hz", "amp_fund",
                     "rms_resid", "rms_resid_rel", "max_resid_rel", "z_actual", "z_pure_measured", "z_resid_added"])
        for r in res_rows:
            wr.writerow([r["run"], r["series"], "%.0f" % r["freq"], r["rep"], r["chan"],
                         "%.6f" % r["f_meas"], "%.6f" % r["offset_hz"], "%.8e" % r["amp_fund"],
                         "%.8e" % r["rms_resid"], "%.6f" % r["rms_resid_rel"],
                         "%.6f" % r["max_resid_rel"], "%.6f" % r["z_actual"],
                         "%.6f" % r["z_pure_measured"], "%.6f" % r["z_resid_added"]])

    if out["errors"]:
        for e in out["errors"]:
            print("ERROR:", e, flush=True)
        sys.exit(1)
    print("TOTALS: rows=%d z>=4=%d npz_ok=%d/%d json_unique=%d" % (
        len(rows), len(high), sum(1 for m in npz_manifest if m["sha256_ok"]), len(npz_manifest), len(seen)), flush=True)
    print("DONE fail-closed clean", flush=True)


if __name__ == "__main__":
    main()
