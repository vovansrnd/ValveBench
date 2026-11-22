#!/usr/bin/env python3
"""v2.26.4 offline forensic audit of four envelope events (D10 -> E-MU 0404).

Read-only: recomputes production envelope_stability() on saved NPZ arrays,
recovers Rig.run() windows, compares submitted_to_backend vs ADC capture.
No ALSA/backend calls. Run from repo root: PYTHONPATH=. python3 <script> --out-dir <dir>
"""
import argparse, csv, hashlib, json, math, os, sys
import numpy as np

from valvebench.gainmatch import envelope_stability, Z_BURST_MAX, TREND_MAX
from valvebench.measure import Rig
from valvebench.backend import SimBackend, SimChannel
from valvebench.calib import Calibration

FS = 192000
DF = 1.0
BLOCKS = 2
SETTLE = 0.5
FREQS = (94.0, 994.0, 9994.0)
REPEATS = 3
SEGN = int(round(SETTLE * FS)) + BLOCKS * int(round(FS / DF)) + int(0.05 * FS)  # 489600
SETTLE_N = int(round(SETTLE * FS))
WIN_N = BLOCKS * int(round(FS / DF))                                            # 384000
SUB_S = 0.1

RUNS = {
    "A_v2261r1": "measurements/diagnostics/v2261-gainmatch-level-margin/results/vovans-PC-run-01",
    "B_v2262run1": "measurements/diagnostics/v2262-gainmatch-repeatability/results/vovans-PC-run-01",
    "C_v2263run2": "measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-02",
    "D_v2263run3": "measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-03",
}

EVENTS = {
    "A_v2261r1": dict(level="m18", series="inject-R", npz="m18-inject_R-R-07.npz",
                      series_json="level_m18/inject-R-series.json", freq=94.0, rep=2, chan="R"),
    "B_v2262run1": dict(level="m18", series="inject-L", npz="m18-inject_L-L-08.npz",
                        series_json="level_m18/inject-L-series.json", freq=9994.0, rep=1, chan="R"),
    "C_v2263run2": dict(level="m20", series="inject-L", npz="m20-inject_L-L-05.npz",
                        series_json="level_m20/inject-L-series.json", freq=94.0, rep=1, chan="R"),
    "D_v2263run3": dict(level="m20", series="inject-L", npz="m20-inject_L-L-05.npz",
                        series_json="level_m20/inject-L-series.json", freq=9994.0, rep=3, chan="R"),
}

HEAD_SHA = "357be1f3ca508e213e0ab59f3ee7f7afe76a36f5"
JSON_READS = []

rig = Rig(SimBackend(SimChannel(gain=0.5, a2=1e-5, a3=1e-5, riaa=False, noise_dbfs=-140),
                     SimChannel(gain=0.5, a2=1e-5, a3=1e-5, riaa=False, noise_dbfs=-140),
                     seed=1), Calibration(), fs=FS, df=DF, blocks=BLOCKS, settle=SETTLE)


def order_of(i):
    """measure_series segment order: rep1 94/994/9994, rep2 ..., rep3 ..."""
    return FREQS[i % 3], i // 3 + 1


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_inputs(out):
    manifest = []
    for tag, run in RUNS.items():
        sj = os.path.join(run, "SHA256SUMS.json")
        JSON_READS.append(sj)
        sums = json.load(open(sj))
        for fp, meta in sums["files"].items():
            if not fp.endswith(".npz"):
                continue
            full = os.path.join(run, fp)
            h, size = sha256_of(full), os.path.getsize(full)
            ok = (h == meta["sha256"] and size == meta["size_bytes"])
            manifest.append(dict(run=tag, file=fp, size_bytes=size,
                                 sha256=h, manifest_ok=ok))
            if not ok:
                out["errors"].append("SHA MISMATCH %s/%s" % (tag, fp))
    json.dump(manifest, open(os.path.join(out["dir"], "INPUT-SHA256SUMS.json"), "w"), indent=1)
    return manifest


def subwindow_detail(win, ch, f0, fs=FS, sub_s=SUB_S):
    """Instrumented copy of envelope_stability internals: 20 LS amplitudes."""
    win = np.asarray(win, dtype=np.float64)
    n_sub = int(round(sub_s * fs))
    t = np.arange(n_sub) / float(fs)
    w = 2.0 * math.pi * f0
    basis = np.column_stack((np.ones(n_sub), np.cos(w * t), np.sin(w * t)))
    pinv = np.linalg.pinv(basis)
    amps = []
    ranges = []
    for s in range(0, win.size - n_sub + 1, n_sub):
        seg = win[s:s + n_sub]
        coef = pinv @ seg
        amps.append(math.hypot(coef[1], coef[2]))
        ranges.append((s, s + n_sub))
    amps = np.asarray(amps)
    mean_amp = max(float(np.mean(amps)), 1e-300)
    k = np.arange(amps.size, dtype=float)
    line = np.polyfit(k, amps, 1)
    trend = np.polyval(line, k)
    resid = amps - trend
    med = float(np.median(resid))
    mad = 1.4826 * float(np.median(np.abs(resid - med)))
    pos_i = int(np.argmax(resid - med)); neg_i = int(np.argmin(resid - med))
    pos_dev = float(resid[pos_i] - med); neg_dev = float(med - resid[neg_i])
    dev = max(pos_dev, neg_dev)
    z = float(dev / max(mad, mean_amp * 1e-6))
    trend_span = float((trend.max() - trend.min()) / mean_amp)
    return dict(amplitudes=[float(a) for a in amps], mean_amp=mean_amp,
                subwindow_ranges=ranges, resid_med=med, mad=mad,
                trend_span=trend_span, z_burst=z, pos_idx=pos_i, pos_dev=pos_dev, pos_dev_pct=100.0 * pos_dev / mean_amp,
                neg_idx=neg_i, neg_dev=neg_dev, neg_dev_pct=100.0 * neg_dev / mean_amp,
                pos_range=ranges[pos_i], neg_range=ranges[neg_i])


def json_repeat(series, freq, rep, chan):
    for r in series["rows"]:
        if abs(r["requested_frequency_hz"] - freq) < 1e-6:
            ch = r["channels"][chan]
            for rp in ch["repeats"]:
                if rp.get("repeat") == rep:
                    return r, ch, rp
    return None, None, None


def fail(out, msg):
    out["errors"].append(msg)
    raise RuntimeError(msg)


def recover_windows(npz_path, out):
    z = np.load(npz_path)
    for key in ("submitted_to_backend", "capture", "fs", "lead_seconds"):
        if key not in z.files:
            fail(out, "npz %s missing key %s" % (npz_path, key))
    submitted = np.asarray(z["submitted_to_backend"], dtype=np.float64)
    capture = np.asarray(z["capture"], dtype=np.float64)
    if capture.ndim != 2 or capture.shape[1] != 2:
        fail(out, "npz %s capture shape %s not (N,2)" % (npz_path, capture.shape))
    if submitted.ndim != 2 or submitted.shape[1] != 2:
        fail(out, "npz %s submitted shape %s not (N,2)" % (npz_path, submitted.shape))
    for name, arr in (("capture", capture), ("submitted", submitted)):
        if not np.isfinite(arr).all():
            fail(out, "npz %s %s has NaN/Inf" % (npz_path, name))
    fs_npz = int(z["fs"])
    if fs_npz != FS:
        fail(out, "npz %s fs=%d != %d" % (npz_path, fs_npz, FS))
    lead_n = int(round(float(z["lead_seconds"]) * fs_npz))
    d = rig._onset(capture, tone_hz=FREQS[0])
    cap_wins, sub_wins = [], []
    for i in range(REPEATS * len(FREQS)):
        a = d + i * SEGN + SETTLE_N
        b = a + WIN_N
        if b > capture.shape[0]:
            fail(out, "npz %s capture window %d out of range" % (npz_path, i))
        w = capture[a:b, :]
        if w.shape != (WIN_N, 2) or not np.isfinite(w).all():
            fail(out, "npz %s capture window %d bad shape/finiteness" % (npz_path, i))
        cap_wins.append(w)
        ai = lead_n + i * SEGN + SETTLE_N
        bi = ai + WIN_N
        if bi > submitted.shape[0]:
            fail(out, "npz %s submitted window %d out of range" % (npz_path, i))
        ws = submitted[ai:bi, :]
        if ws.shape != (WIN_N, 2) or not np.isfinite(ws).all():
            fail(out, "npz %s submitted window %d bad shape/finiteness" % (npz_path, i))
        sub_wins.append(ws)
    return dict(onset=d, lead_n=lead_n, fs=fs_npz, cap_wins=cap_wins, sub_wins=sub_wins,
                capture=capture, submitted=submitted)


def series_for_npz(run, level, series_name):
    p = os.path.join(run, "level_%s" % level, "%s-series.json" % series_name)
    return json.load(open(p))


def analyze_window(win, ch_index, freq, series=None, rep=None, chan_name=None):
    ch_names = ("L", "R")
    sig = np.asarray(win[:, ch_index], dtype=np.float64)
    k = int(round(freq * DF))
    actual_f = rig.f_of(k)
    prod = envelope_stability(sig, FS, actual_f)
    detail = subwindow_detail(sig, ch_index and 0 or 0, actual_f)  # ch not used inside
    an = rig._tone_an(win, ch_index, k, nharm=9)
    f0_meas = float(an["f0"])
    alt = envelope_stability(sig, FS, f0_meas)
    jr = jc = None
    if series is not None:
        _, jc, jr = json_repeat(series, freq, rep, chan_name)
    rec = dict(freq=freq, rep=rep, chan=chan_name,
               commanded_f=actual_f, measured_f0=f0_meas,
               prod_z=prod["z_burst"], prod_trend=prod["trend_span"], prod_sub=prod["subwindows"],
               alt_z_measured_f0=alt["z_burst"], alt_trend_measured_f0=alt["trend_span"],
               json_z=(jr or {}).get("envelope", {}).get("z_burst"),
               json_trend=(jr or {}).get("envelope", {}).get("trend_span"),
               json_valid=(jr or {}).get("valid"),
               json_reasons=(jr or {}).get("validity_reasons"),
               json_cr=(jr or {}).get("carrier_residual_db"),
               json_sinad=(jr or {}).get("sinad_db"),
               json_noise=(jr or {}).get("noise_dbfs"),
               json_level=(jr or {}).get("level_dbfs"),
               json_peak=(jr or {}).get("peak_dbfs"),
               detail=detail)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    out = dict(errors=[], events={}, global_z=[], dir=args.out_dir)
    manifest = verify_inputs(out)

    # ---- global recompute over all non-preflight captures of the four runs
    captures = {}
    for tag, run in RUNS.items():
        sums = json.load(open(os.path.join(run, "SHA256SUMS.json")))
        npzs = [fp for fp in sums["files"] if fp.endswith(".npz") and "preflight" not in fp]
        for fp in sorted(npzs):
            base = os.path.basename(fp)
            level = "m" + base.split("-")[0][1:]
            if "loopback-none" in base:
                sname = "loopback"
            elif "inject_R" in base:
                sname = "inject-R"
            elif "inject_L" in base:
                sname = "inject-L"
            else:
                out["errors"].append("unclassified npz %s" % fp); continue
            series = series_for_npz(run, level, sname)
            JSON_READS.append(os.path.join(run, "level_%s" % level, "%s-series.json" % sname))
            w = recover_windows(os.path.join(run, fp), out)
            captures[(tag, base)] = w
            rows = []
            for i in range(REPEATS * len(FREQS)):
                freq, rep = order_of(i)
                for ci, cname in ((0, "L"), (1, "R")):
                    rec = analyze_window(w["cap_wins"][i], ci, freq, series, rep, cname)
                    rec["run"] = tag; rec["npz"] = base; rec["level"] = level
                    rec["series"] = sname; rec["seg_index"] = i
                    rows.append(rec)
                    out["global_z"].append(dict(run=tag, npz=base, series=sname, level=level,
                                                freq=freq, rep=rep, chan=cname,
                                                z=rec["prod_z"], trend=rec["prod_trend"],
                                                json_z=rec["json_z"],
                                                match=(rec["json_z"] is not None and
                                                       abs(rec["prod_z"] - rec["json_z"]) < 1e-6)))
            mism = [r for r in rows if r["json_z"] is None or abs(r["prod_z"] - r["json_z"]) > 1e-6]
            print("[%s/%s] windows recomputed: %d, JSON z-burst mismatches: %d" %
                  (tag, base, len(rows), len(mism)), flush=True)
            for r in mism:
                out["errors"].append("z mismatch %s/%s seg%d %s %.0fHz rep%d: prod=%.6f json=%s" % (
                    tag, base, r["seg_index"], r["chan"], r["freq"], r["rep"], r["prod_z"], r["json_z"]))
                print("   MISMATCH seg%d %s %.0f Hz rep%d: prod_z=%.6f json_z=%s" %
                      (r["seg_index"], r["chan"], r["freq"], r["rep"], r["prod_z"], r["json_z"]), flush=True)
            out["events"].setdefault(tag, {}).setdefault("series", {})[base] = rows

    # ---- event cards
    for tag, ev in EVENTS.items():
        run = RUNS[tag]
        w = captures.get((tag, ev["npz"]))
        series = series_for_npz(run, ev["level"], ev["series"])
        i = [j for j in range(REPEATS * len(FREQS)) if order_of(j) == (ev["freq"], ev["rep"])][0]
        JSON_READS.append(os.path.join(run, "clock-preflight.json"))
        JSON_READS.append(os.path.join(run, "run-summary.json"))
        ci = 1 if ev["chan"] == "R" else 0
        jrow, jchan, jrep = json_repeat(series, ev["freq"], ev["rep"], ev["chan"])
        cap_rec = analyze_window(w["cap_wins"][i], ci, ev["freq"], series, ev["rep"], ev["chan"])
        sub_rec = subwindow_detail(w["sub_wins"][i][:, ci], 0, cap_rec["commanded_f"])
        cap_rec["submitted_detail"] = sub_rec
        card = dict(json_event=dict(z_burst=(jrep or {}).get("envelope", {}).get("z_burst"),
                                    trend_span=(jrep or {}).get("envelope", {}).get("trend_span"),
                                    subwindows=(jrep or {}).get("envelope", {}).get("subwindows"),
                                    validity_reasons=(jrep or {}).get("validity_reasons"),
                                    carrier_residual_db=(jrep or {}).get("carrier_residual_db"),
                                    sinad_db=(jrep or {}).get("sinad_db"),
                                    noise_dbfs=(jrep or {}).get("noise_dbfs"),
                                    level_dbfs=(jrep or {}).get("level_dbfs"),
                                    peak_dbfs=(jrep or {}).get("peak_dbfs"),
                                    clipped=(jrep or {}).get("clipped")),
                    recompute_capture=dict(z=cap_rec["prod_z"], trend=cap_rec["prod_trend"],
                                           sub=cap_rec["prod_sub"],
                                           match_json=(jrep is not None and abs(cap_rec["prod_z"] - (jrep or {}).get("envelope", {}).get("z_burst", 0)) < 1e-6),
                                           pos_subwindow=cap_rec["detail"]["pos_idx"],
                                           pos_dev_pct=cap_rec["detail"]["pos_dev_pct"],
                                           neg_subwindow=cap_rec["detail"]["neg_idx"],
                                           neg_dev_pct=cap_rec["detail"]["neg_dev_pct"],
                                           pos_range=cap_rec["detail"]["pos_range"],
                                           neg_range=cap_rec["detail"]["neg_range"],
                                           onset_sample=w["onset"], lead_n=w["lead_n"],
                                           window_start=w["onset"] + i * SEGN + SETTLE_N),
                    recompute_submitted=dict(z=sub_rec["z_burst"], trend=sub_rec["trend_span"],
                                             pos_subwindow=sub_rec["pos_idx"],
                                             pos_dev_pct=sub_rec["pos_dev_pct"],
                                             neg_subwindow=sub_rec["neg_idx"],
                                             neg_dev_pct=sub_rec["neg_dev_pct"]),
                    alt_measured_f0=dict(z=cap_rec["alt_z_measured_f0"], trend=cap_rec["alt_trend_measured_f0"],
                                         measured_f0=cap_rec["measured_f0"]),
                    amplitudes_capture=cap_rec["detail"]["amplitudes"],
                    amplitudes_submitted=sub_rec["amplitudes"])
        out["events"][tag]["event"] = card
        print("[EVENT %s] json_z=%s recomputed_capture_z=%.6f match=%s | submitted_z=%.6f | pos subwin %d (%.3f%%) neg %d (%.3f%%)" %
              (tag, card["json_event"]["z_burst"], card["recompute_capture"]["z"],
               card["recompute_capture"]["match_json"], card["recompute_submitted"]["z"],
               card["recompute_capture"]["pos_subwindow"], card["recompute_capture"]["pos_dev_pct"],
               card["recompute_capture"]["neg_subwindow"], card["recompute_capture"]["neg_dev_pct"]), flush=True)

    # ---- CSV: event subwindow amplitudes (capture + submitted)
    with open(os.path.join(args.out_dir, "EVENT-SUBWINDOW-AMPLITUDES.csv"), "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["event", "source", "subwindow", "amplitude", "mean_amp"])
        for tag, ev in out["events"].items():
            card = ev["event"]
            for j, a in enumerate(card["amplitudes_capture"]):
                wr.writerow([tag, "capture", j, a, card["amplitudes_capture"][j] and np.mean(card["amplitudes_capture"])])
            for j, a in enumerate(card["amplitudes_submitted"]):
                wr.writerow([tag, "submitted", j, a, np.mean(card["amplitudes_submitted"])])

    # ---- CSV: control matrix rows (all recomputed windows)
    with open(os.path.join(args.out_dir, "CONTROL-MATRIX.csv"), "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["run", "npz", "level", "series", "seg", "freq", "rep", "chan",
                     "prod_z", "prod_trend", "json_z", "z_match", "json_valid", "json_reasons",
                     "json_cr", "json_sinad", "pos_dev_pct", "neg_dev_pct"])
        for tag in RUNS:
            for base, rows in out["events"][tag]["series"].items():
                for r in rows:
                    wr.writerow([tag, base, r["level"], r["series"], r["seg_index"],
                                 "%.0f" % r["freq"], r["rep"], r["chan"],
                                 "%.6f" % r["prod_z"], "%.8f" % r["prod_trend"],
                                 r["json_z"], r["json_z"] is not None and abs(r["prod_z"] - r["json_z"]) < 1e-6,
                                 r["json_valid"], "|".join(r["json_reasons"] or []),
                                 r["json_cr"], r["json_sinad"],
                                 "%.4f" % r["detail"]["pos_dev_pct"], "%.4f" % r["detail"]["neg_dev_pct"]])

    seen = {}
    for jp in sorted(set(JSON_READS)):
        seen[jp] = dict(size_bytes=os.path.getsize(jp), sha256=sha256_of(jp))
    json.dump(dict(pinned_commit=HEAD_SHA, description="all unique JSON files actually read by audit script",
                   files=seen),
              open(os.path.join(args.out_dir, "INPUT-JSON-SHA256SUMS.json"), "w"), indent=1)
    print("JSON inventory: %d unique files hashed" % len(seen), flush=True)

    n_rows = len(out["global_z"])
    n_z4 = sum(1 for g in out["global_z"] if g["z"] >= 4.0)
    all_match = all(g["match"] for g in out["global_z"])
    n_npz_checked = sum(1 for m in manifest if m["file"].endswith(".npz"))
    n_npz_ok = sum(1 for m in manifest if m["file"].endswith(".npz") and m["manifest_ok"])
    print("TOTALS: rows=%d (expect 324) z>=4.0=%d (expect 6) all_z_match=%s npz_hash_ok=%d/%d" %
          (n_rows, n_z4, all_match, n_npz_ok, n_npz_checked), flush=True)
    if n_rows != 324:
        out["errors"].append("row count %d != 324" % n_rows)
    if n_z4 != 6:
        out["errors"].append("z>=4.0 count %d != 6" % n_z4)
    if not all_match:
        out["errors"].append("not all z_match")
    if n_npz_ok != 26 or n_npz_checked != 26:
        out["errors"].append("npz hash checks %d/%d != 26" % (n_npz_ok, n_npz_checked))
    if out["errors"]:
        for e in out["errors"]:
            print("ERROR:", e, flush=True)
        print("FAIL-CLOSED: %d errors -> exit 1" % len(out["errors"]), flush=True)
        json.dump(out, open(os.path.join(args.out_dir, "audit-results.json"), "w"), indent=1, default=float)
        sys.exit(1)
    json.dump(out, open(os.path.join(args.out_dir, "audit-results.json"), "w"), indent=1, default=float)
    print("DONE. errors: 0 (fail-closed clean)", flush=True)


if __name__ == "__main__":
    main()
