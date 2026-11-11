"""Independent regressions for issues found while reviewing 0.1.20-dev3."""
from __future__ import annotations

import math
import os
import sys
import json
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))

from valvebench import peakhold as ph
from valvebench.glitch_test import scan_segment, _FileSource, analyse_recording
from valvebench.pcm import to_pcm

passed = 0

# ============================================================
# T1-R (0.1.20-dev3-reviewfix2, ТЗ §5.1): независимая проверка SINAD
# Несущая −20 dBFS, H2 = −32 dBc → −52 dBFS, H3 = −40 dBc → −60 dBFS.
# Аналитика: SINAD = −20 − 10lg(10^(−5.2) + 10^(−6.0)) = 31.36 dB.
# Предыдущая версия теста ошибочно задавала H2/H3 как абсолютные −32/−40 dBFS.
# ============================================================
import math as _math

FS_T1 = 48000
N_T1 = int(0.6 * FS_T1)
CARRIER_DB, H2_DBC, H3_DBC = -20.0, -32.0, -40.0
F0_T1 = 1000.0


def _t1_signal():
    t = np.arange(N_T1) / FS_T1
    a_c = 10 ** (CARRIER_DB / 20)
    a_h2 = a_c * 10 ** (H2_DBC / 20)
    a_h3 = a_c * 10 ** (H3_DBC / 20)
    x = (a_c * np.sin(2 * np.pi * F0_T1 * t)
         + a_h2 * np.sin(2 * np.pi * 2 * F0_T1 * t + 0.7)
         + a_h3 * np.sin(2 * np.pi * 3 * F0_T1 * t + 1.9))
    return x, a_c, a_h2, a_h3


def _t1_reference(x):
    """Независимый SINAD: LSQ-проекция H1..H3, остаток = негармонический."""
    t = np.arange(x.size) / FS_T1
    w = 2 * np.pi * F0_T1
    cols = [np.ones_like(t)]
    for h in range(1, 4):
        cols += [np.cos(h * w * t), np.sin(h * w * t)]
    A = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(A, x, rcond=None)
    amp = [math.hypot(coef[1 + 2 * i], coef[2 + 2 * i]) for i in range(3)]
    res = x - A @ coef
    p_sig = (amp[0] ** 2) / 2
    p_dist = sum((a ** 2) / 2 for a in amp[1:])
    p_res = float(np.mean(res * res))
    sinad = 10 * math.log10(p_sig / (p_dist + p_res))
    return sinad, amp


def test_t1_sinad_static_live_reference():
    x, a_c, a_h2, a_h3 = _t1_signal()
    # 1) абсолютные уровни гармоник: H2 → −52 dBFS, H3 → −60 dBFS
    ok_levels = (abs(20 * math.log10(a_h2 / a_c) - H2_DBC) < 1e-9
                 and abs(20 * math.log10(a_h3 / a_c) - H3_DBC) < 1e-9)
    # 2) аналитика
    p_h2, p_h3 = 10 ** ((CARRIER_DB + H2_DBC) / 10), 10 ** ((CARRIER_DB + H3_DBC) / 10)
    sinad_analytic = CARRIER_DB - 10 * math.log10(p_h2 + p_h3)
    ok_analytic = abs(sinad_analytic - 31.36) < 0.01
    # 3) независимый LSQ-референс на сигнале
    sinad_ref, amps = _t1_reference(x)
    ok_ref = abs(sinad_ref - sinad_analytic) < 0.05
    # 4) Static: measure_series на этом сигнале (симуляция серии через SimBackend не
    #    нужна — спектральный анализатор применяется к готовому окну)
    from valvebench import dsp as _dsp
    an = _dsp.analyze_tone(x, FS_T1, F0_T1, N_T1, nharm=9, mains=0.0)
    # SINAD продукта = −thdn_db (тон/шум+искажения в полосе)
    sinad_static = -float(an["thdn_db"])
    # 5) Live: GainTracker C/R и SINAD на том же окне
    from valvebench.gainmatch import GainTracker, tone_fit_harmonics
    tr = GainTracker()
    ring = np.column_stack([x, x])
    st = tr.update(ring, FS_T1, F0_T1, CARRIER_DB, 0, None, now=0.0)
    sinad_live = min(st["sinad_db"])
    # 6) подпись в gainview — SINAD, не SNR
    import valvebench.gainview as gv
    gv_src = open(gv.__file__, encoding="utf-8").read()
    label_ok = "SINAD" in gv_src and "SNR " not in gv_src.replace("SINAD", "")
    ok = (ok_levels and ok_analytic and ok_ref
          and abs(sinad_static - 31.36) < 0.5
          and abs(sinad_live - 31.36) < 0.5
          and label_ok)
    detail = (f"аналитика={sinad_analytic:.2f}; LSQ-референс={sinad_ref:.2f}; "
              f"Static={sinad_static:.2f}; Live={sinad_live:.2f}; "
              f"уровни H2/H3 корректны={ok_levels}; подпись SINAD в UI={label_ok}")
    check("T1-R: SINAD −20 dBFS + H2 −32 dBc + H3 −40 dBc ≈ 31.36 дБ "
          "(аналитика=Static=Live, подпись UI — SINAD)", ok, detail)


def check(name, cond, detail=""):
    global passed
    print(("PASS " if cond else "FAIL ") + name + (f": {detail}" if detail else ""))
    if not cond:
        raise SystemExit(1)
    passed += 1


test_t1_sinad_static_live_reference()


# 1. Coherent transfer estimates for L->R and R->L are independent.
fs, n = 96000, 65536
t = np.arange(n) / fs
acc = ph.PeakHoldAccumulator(12)
for l_db, r_db in ((-10.0, -70.0), (-30.0, -9.9)):
    x = np.column_stack((
        10.0 ** (l_db / 20.0) * np.sin(2 * np.pi * 1000 * t + 0.2),
        10.0 ** (r_db / 20.0) * np.sin(2 * np.pi * 1000 * t + 0.8),
    ))
    r = ph.rta_job(x, fs, n, 12)
    acc.update(r["bands"], r["tone"])
i = acc.band_index(1000.0)
hold = acc.tone_hold()
measured = float(hold[0, i] - hold[1, i])
check("directional coherence: changing dominant channel does not mix L→R and R→L",
      acc.drv_idx[i] == 1 and abs(measured + 20.1) < 0.15, f"R leads; measured R→L={measured:.2f} dB")


# 2. Corrected CSV computes cross-talk from corrected, not raw, levels.
centers = np.array([500.0, 1000.0, 2000.0])
levels = np.array([[-20.0, -20.0, -20.0], [-40.0, -40.0, -40.0]])
corr = np.array([[-5.0, -5.0, -5.0], [0.0, 0.0, 0.0]])
ov = ph.Overlay("corrected", ph.PALETTE[0], "tone", 12, 20.0, 20000.0, fs,
                centers, levels, corr_db=corr)
lines = [line for line in ph.overlays_csv([ov], corrected=True).splitlines() if not line.startswith("#")]
header = lines[0].split(",")
row = lines[1].split(",")
xtalk_value = float(row[header.index('"corrected xtalk(other-driven)"')])
check("corrected CSV transfer: cross-talk follows corrected L/R columns", abs(xtalk_value + 25.0) < 1e-6,
      f"exported {xtalk_value:.2f} dB; expected -25.00 dB")


# 3. Normalization is exact at 1 kHz even when no fractional-octave centre is 1 kHz.
for po in (3, 6, 12, 24):
    edges = ph.band_edges(po)
    f = ph.band_centers(edges)
    # An exact 4 dB/octave straight line with its physical 0 dB crossing at 1 kHz.
    line = 4.0 * np.log2(f / 1000.0)
    normalized = ph.relative_levels(f, np.vstack((line, line - 20.0)))
    r0 = ph.reference_level(f, normalized[0], 1000.0)
    r1 = ph.reference_level(f, normalized[1], 1000.0)
    check(f"log-frequency normalization at 1 kHz ({po}/oct)", abs(r0) < 1e-9 and abs(r1 + 20.0) < 1e-9,
          f"L@1k={r0:+.6f}; R@1k={r1:+.6f} dB")



# 3b. Per-band driver direction survives snapshots and determines sign of cross-talk.
centers_dir = np.array([500.0, 1000.0, 2000.0, 4000.0])
levels_dir = np.array([[-10.0, -12.0, -40.0, -45.0],
                       [-40.0, -37.0, -10.0, -10.0]])
driven_dir = np.array([0, 0, 1, 1], dtype=int)
_, xt_dir = ph.split_driven(levels_dir, driven_dir)
check("cross-talk direction is evaluated per band when channel response curves cross",
      np.allclose(xt_dir, [-30.0, -25.0, -30.0, -35.0]),
      f"per-band other−driven={xt_dir.tolist()}")
cross_centers = np.array([100.0, 200.0, 400.0, 800.0, 1600.0, 3200.0, 6400.0])
cross_levels = np.array([[-10.0, -11.0, -12.0, -13.0, -50.0, -51.0, -52.0],
                         [-40.0, -41.0, -42.0, -43.0, -9.0, -10.0, -11.0]])
cross_dirs = np.array([0, 0, 0, 0, 1, 1, 1])
cross_norm = ph.relative_levels(cross_centers, cross_levels, driven=cross_dirs)
cross_l1k = ph.reference_level(cross_centers, cross_norm[0], 1000.0)
cross_r1k = ph.reference_level(cross_centers, cross_norm[1], 1000.0)
check("crossing response curves are normalized independently, not misclassified as one channel leakage",
      abs(cross_l1k or 0.0) < 1e-9 and abs(cross_r1k or 0.0) < 1e-9,
      f"L@1k={cross_l1k}; R@1k={cross_r1k}")
ov_dir = ph.Overlay("crossing", ph.PALETTE[0], "tone", 12, 20.0, 20000.0, fs,
                    centers_dir, levels_dir, driven=driven_dir)
roundtrip = ph.Overlay.from_json(ov_dir.to_json())
check("per-band driver direction persists through JSON overlay storage",
      np.array_equal(roundtrip.driven, driven_dir), str(roundtrip.driven))
acc_cross = ph.PeakHoldAccumulator(12)
for f, lvl, drv in ((500.0, [-10.0, -40.0], 0), (2000.0, [-40.0, -10.0], 1)):
    acc_cross.update(None, {"f": f, "lvl": np.asarray(lvl), "driven": drv,
                            "num": 10.0 ** (-30.0 / 20.0) + 0j, "den": 1.0})
snap_cross = acc_cross.snapshot("crossing", ph.PALETTE[0], "tone", 96000.0)
check("Peak Hold snapshot takes per-band driver state from the live accumulator",
      snap_cross.driven[acc_cross.band_index(500.0)] == 0
      and snap_cross.driven[acc_cross.band_index(2000.0)] == 1, str(snap_cross.driven.tolist()))
csv_dir = ph.overlays_csv([ov_dir], corrected=False)
rows_dir = [line for line in csv_dir.splitlines() if not line.startswith("#")]
h_dir = rows_dir[0].split(",")
xt_col = h_dir.index('"crossing xtalk(other-driven)"')
xt_dir_csv = [float(line.split(",")[xt_col]) for line in rows_dir[1:]]
check("Peak Hold CSV exports direction-correct cross-talk for every band",
      np.allclose(xt_dir_csv, [-30.0, -25.0, -30.0, -35.0]), str(xt_dir_csv))

# A partial sweep that never reaches 1 kHz must not be normalized to its nearest band.
partial_f = np.array([100.0, 200.0, 400.0])
partial_row = np.array([-30.0, -25.0, -20.0])
check("partial capture without 1 kHz returns no reference level",
      ph.reference_level(partial_f, partial_row, 1000.0) is None, "no 1 kHz coverage")
partial_norm = ph.relative_levels(partial_f, np.vstack((partial_row, partial_row - 15.0)))
check("relative normalization leaves an out-of-range partial curve raw rather than falsely zeroing a band",
      np.allclose(partial_norm[0], partial_row) and np.allclose(partial_norm[1], partial_row - 15.0),
      str(partial_norm.tolist()))
partial_ov = ph.Overlay("partial", ph.PALETTE[1], "tone", 12, 20.0, 20000.0, fs,
                       partial_f, np.vstack((partial_row, partial_row - 15.0)), driven=np.array([0, 0, 0]))
partial_csv = ph.overlays_csv([partial_ov], normalize=True)
check("CSV metadata says normalization unavailable when partial curve has no 1 kHz reference",
      "requested-but-unavailable-no-1kHz-data" in partial_csv, partial_csv.splitlines()[0])

# 4. A dropout crossing arbitrary 2.7 s analysis-block boundaries retains its duration.
fs, f0, seconds = 48000, 997.0, 12.0
t = np.arange(int(seconds * fs)) / fs
base = 0.2 * np.sin(2 * np.pi * f0 * t)
for start, duration in ((2.65, 0.3), (2.65, 2.0), (2.65, 4.0),
                        (5.35, 0.3), (5.35, 2.0), (5.35, 4.0)):
    x = np.column_stack((base.copy(), np.zeros_like(base)))
    a = int(round(start * fs)); b = int(round((start + duration) * fs))
    x[a:b, 0] = 0.0
    events = scan_segment(x, fs, f0, (True, False), 0.0)
    drops = [e for e in events if e["adc"] == "L" and e["class"] == "dropout"]
    ok = (len(drops) == 1 and abs(drops[0]["time_s"] - start) < 0.004
          and abs(drops[0]["duration_ms"] - duration * 1000.0) < 8.0)
    check(f"block boundary dropout {duration:g}s at {start:g}s", ok,
          f"events={[(round(e['time_s'],3),round(e['duration_ms'],1)) for e in drops]}")

silent = np.zeros((int(4.0 * fs), 2), dtype=float)
silent_events = scan_segment(silent, fs, f0, (True, False), 0.0)
check("no captured carrier is reported as unverifiable, not as zero dropouts",
      any(e["adc"] == "L" and e["class"] == "carrier_unverified" for e in silent_events),
      str(silent_events[:1]))

# The streaming path must also preserve the user's --keep-wav request.
with tempfile.TemporaryDirectory(prefix="vb-dev3-keepwav-") as td:
    td = Path(td)
    raw_path = td / "capture.raw"
    fs_io = 48000
    zero_capture = np.zeros((27 * fs_io, 2), dtype=np.float64)
    raw_path.write_bytes(to_pcm(zero_capture, "S24_3LE"))
    source = _FileSource(raw_path, "S24_3LE", 2)
    outdir = td / "out"
    outdir.mkdir()
    analyse_recording(source, fs_io, "L", -18.0, 10.0, 997.0, 6.0, 1.0, 1.0, 24.0,
                      {"playback": [], "capture": []}, outdir, keep_wav=True,
                      raw_path=raw_path, raw_fmt="S24_3LE", raw_channels=2, print_fn=lambda _s: None)
    report = json.loads((outdir / "glitch.json").read_text(encoding="utf-8"))
    check("streaming glitch analysis exports --keep-wav and marks missing carrier as unverifiable",
          (outdir / "capture-float32.wav").is_file()
          and report["adc"]["L"]["carrier_reference"] == "missing",
          f"wav={(outdir / 'capture-float32.wav').stat().st_size} bytes; reference={report['adc']['L']['carrier_reference']}")


# 5. Peak Hold correction grid is rebuilt on resolution changes; Save rejects stale context.
import stubgi
stubgi.install()
from valvebench.ui import PeakHoldPage

class Select:
    def __init__(self, value): self.value = value
    def get_selected(self): return self.value

class Label:
    def set_text(self, value): self.value = value

profile = {"kind": "frequency_response_calibration", "channels": {
    "L": {"frequency_hz": [20.0, 1000.0, 20000.0], "fr_norm_db": [-1.0, 0.0, -2.0]},
    "R": {"frequency_hz": [20.0, 1000.0, 20000.0], "fr_norm_db": [-1.0, 0.0, -2.0]},
}}
class FakeWindow:
    def __init__(self): self.messages=[]
    def rig_fs(self): return 96000
    def get_response_calibration(self, fs): return profile, "test profile"
    def toast(self, msg): self.messages.append(msg)

page = object.__new__(PeakHoldPage)
page.win = FakeWindow()
page.res = Select(3)  # 1/24 octave
page.corr_mode = Select(1)
page.corr_label = Label()
page.acc = ph.PeakHoldAccumulator(12)
page.acc.frames = 2
page._corr = ph.correction_curve(profile, page.acc.centers)
page._profile_note = ""
page.last_res = object()
page._rate = 1.0
page._tone_hist = (1.0, 1000.0)
page._lf_limit = 20.0
page._redraw = lambda: None
PeakHoldPage._on_resolution(page)
check("resolution change rebuilds correction curve to match the new band grid",
      page._corr is not None and page._corr.shape == (2, page.acc.centers.size),
      f"corr={None if page._corr is None else page._corr.shape}, centers={page.acc.centers.size}")

page = object.__new__(PeakHoldPage)
page.win = FakeWindow()
page.ctx = ("usb:old", 96000, "mixer-old")
page.acc = ph.PeakHoldAccumulator(12)
page.acc.update(None, {"f": 1000.0, "lvl": np.array([-20.0, -60.0]), "driven": 0,
                       "num": 0.001 + 0j, "den": 1.0})
page.overlays = []
page.mode_key = lambda: "tone"
page._context = lambda fresh_mixer=False: ("usb:old", 96000, "mixer-new")
page._refresh_correction = lambda: None
page._redraw = lambda: None
page.rejected = 0
PeakHoldPage.on_save(page)
check("Save refuses a curve when the hardware mixer context changed since capture",
      page.acc.frames == 0 and not page.overlays and bool(page.win.messages),
      page.win.messages[-1] if page.win.messages else "no message")

# _context(fresh_mixer=True) bypasses the regular three-second UI cache.
import valvebench.ui as ui
fake = FakeWindow()
fake.cards = [("hw:1,0", "E-MU")]
fake.dev = Select(0)
fake._mixer_fingerprint_cache = {}
fake._cached_mixer_fingerprint = lambda dev: ("stale",)
fake.rig_fs = lambda: 96000
page = object.__new__(PeakHoldPage)
page.win = fake
original = ui.gainmatch.mixer_fingerprint_from_hw
calls = []
ui.gainmatch.mixer_fingerprint_from_hw = lambda dev: (calls.append(dev) or ("fresh",))
try:
    fresh = PeakHoldPage._context(page, fresh_mixer=True)
finally:
    ui.gainmatch.mixer_fingerprint_from_hw = original
expected = __import__("hashlib").sha1(repr(("fresh",)).encode("utf-8")).hexdigest()[:10]
check("Save context bypasses the 3-second mixer cache and refreshes it",
      calls == ["hw:1,0"] and fresh[2] == expected
      and fake._mixer_fingerprint_cache["hw:1,0"][1] == ("fresh",), str(fresh))


print(f"\n{passed} dev3 review-fix checks passed")
