#!/usr/bin/env python3
"""v2.23.0 — narrowband pilot occupancy on realistic synthetic capture noise.

Exercises production Rig.run()/clock_check() offline through a synthetic
backend. Reports a 10 ms coherent-demodulation envelope independently for
L/R and each 1 s analysis block, plus occupancy and longest low-envelope run.
The 0.97 occupancy value and 50 ms run length are candidates only: no gate,
threshold, pilot, hardware, or production code is modified.
"""
from __future__ import annotations
import json, math, platform, sys
from pathlib import Path
import numpy as np
import scipy
from scipy.signal import hilbert

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from valvebench.measure import Rig, clock_check

FS = 96000
WINDOW_S = 2.0
SETTLE_S = 0.4
DELAY_S = 0.137
FRAME_S = 0.010
HOP_S = 0.002
NOISE_DBFS = -80.0             # sine-equivalent broadband RMS level
LEAKAGE_DBFS = -109.0          # observed 1 kHz line in quiet L channel
PILOT_LEVELS = (-66.0, -72.0)
PILOT_HZ = 1000.0
SEED_BASE = 223001


def amp(db): return 10.0 ** (float(db) / 20.0)
def finite(x):
    try:
        y=float(x)
        return y if math.isfinite(y) else None
    except (TypeError, ValueError): return None

def make_case(name, intervals=None, ppm=0.0, am_depth=0.0, am_hz=0.0,
              noise_dbfs=NOISE_DBFS, leakage_dbfs=LEAKAGE_DBFS, pilot_dbfs=-72.0):
    if intervals is None: intervals=[[0.0, WINDOW_S]]
    return dict(name=name, active_intervals_s=intervals, frequency_offset_ppm=ppm,
                am_depth=am_depth, am_hz=am_hz, noise_dbfs=noise_dbfs,
                leakage_dbfs=leakage_dbfs, pilot_dbfs=pilot_dbfs)

CASES=[]
for level in PILOT_LEVELS:
    CASES += [
        make_case(f"steady_{level:g}dBFS", pilot_dbfs=level),
        make_case(f"tail_gap_150ms_{level:g}dBFS", [[0,1.85]], pilot_dbfs=level),
        make_case(f"tail_gap_350ms_{level:g}dBFS", [[0,1.65]], pilot_dbfs=level),
        make_case(f"tail_gap_800ms_{level:g}dBFS", [[0,1.20]], pilot_dbfs=level),
        make_case(f"front_40pct_{level:g}dBFS", [[0,0.80]], pilot_dbfs=level),
        make_case(f"front_49pct_{level:g}dBFS", [[0,0.98]], pilot_dbfs=level),
        make_case(f"split_40pct_{level:g}dBFS", [[0,0.40],[1.0,1.40]], pilot_dbfs=level),
    ]
for level in PILOT_LEVELS:
    for gap_ms in (20, 40, 50, 80, 100):
        period=0.200
        on=max(0.0, period-gap_ms/1000.0)
        intervals=[]; t=0.0
        while t < WINDOW_S-1e-12:
            if on>0: intervals.append([t,min(WINDOW_S,t+on)])
            t+=period
        CASES.append(make_case(f"periodic_gap_{gap_ms}ms_{level:g}dBFS", intervals, pilot_dbfs=level))
for depth, hz in ((0.2,5.0),(0.5,5.0),(0.5,20.0),(0.8,2.0)):
    CASES.append(make_case(f"AM_{depth:g}_{hz:g}Hz", am_depth=depth, am_hz=hz, pilot_dbfs=-72.0))
for ppm in (-100.0,-50.0,0.0,50.0,100.0):
    CASES.append(make_case(f"clock_offset_{ppm:+g}ppm", ppm=ppm, pilot_dbfs=-72.0))
CASES.append(make_case("noise_only", intervals=[], pilot_dbfs=-72.0))


class SyntheticBackend:
    name="v2230-synthetic-realistic-noise"
    def __init__(self,cfg,seed): self.cfg=cfg; self.seed=seed; self.fs=FS; self.notes=[]; self.meta={}
    def open(self,fs): self.fs=int(fs)
    def play_record(self,out,tail=0.8):
        program=np.asarray(out,dtype=np.float64)
        if program.ndim==1: program=np.column_stack((program,program))
        ppm=float(self.cfg.get("frequency_offset_ppm",0.0))
        if ppm:
            src=np.arange(len(program),dtype=float)*(1.0+ppm*1e-6)
            ix=np.arange(len(program),dtype=float)
            program=np.column_stack([np.interp(src,ix,program[:,c],left=0.0,right=0.0) for c in (0,1)])
        start=int(round(SETTLE_S*self.fs)); count=int(round(WINDOW_S*self.fs)); end=start+count
        env=np.zeros(count,dtype=float)
        for a,b in self.cfg["active_intervals_s"]:
            lo=max(0,int(round(a*self.fs))); hi=min(count,int(round(b*self.fs)))
            if hi>lo: env[lo:hi]=1.0
        depth=float(self.cfg.get("am_depth",0.0)); mod_hz=float(self.cfg.get("am_hz",0.0))
        if depth and mod_hz:
            tm=np.arange(count)/self.fs
            env*=1.0+depth*np.sin(2*np.pi*mod_hz*tm)
        program[start:end,:]*=env[:,None]
        # Correlated broadband noise: independent Gaussian components with rho=.95.
        rng=np.random.default_rng(self.seed)
        n=int(round(DELAY_S*self.fs))+len(program)+int(round((tail+0.3)*self.fs))
        z0=rng.standard_normal(n); z1=rng.standard_normal(n)
        rho=0.95
        noise=np.column_stack((z0,rho*z0+math.sqrt(1-rho*rho)*z1))
        noise*=amp(float(self.cfg.get("noise_dbfs",NOISE_DBFS)))/math.sqrt(2.0)
        rec=noise
        delay=int(round(DELAY_S*self.fs))
        rec[delay:delay+len(program)] += program
        # Persistent narrow 1 kHz leakage line, independent of pilot occupancy.
        t=np.arange(n)/self.fs
        leak=amp(float(self.cfg.get("leakage_dbfs",LEAKAGE_DBFS)))
        rec[:,0]+=leak*np.sin(2*np.pi*1000.0*t+0.31)
        rec[:,1]+=amp(-125.0)*np.sin(2*np.pi*1000.0*t+1.13)
        self.meta={"noise_dbfs":self.cfg.get("noise_dbfs"),"noise_channel_correlation":rho,
                   "leakage_L_dbfs":self.cfg.get("leakage_dbfs"),"leakage_R_dbfs":-125.0,
                   "delay_s":DELAY_S,"program_window_start_s":SETTLE_S}
        self.last_recording=rec
        return rec


def envelope_frames(x,fs,f):
    n=int(round(FRAME_S*fs)); hop=int(round(HOP_S*fs))
    if len(x)<n: return np.array([]),np.array([])
    # Sliding coherent demodulation. Frame phases reset independently; magnitude is phase-invariant.
    starts=np.arange(0,len(x)-n+1,hop,dtype=int)
    ref=np.exp(-2j*np.pi*f*np.arange(n)/fs)
    vals=np.empty(len(starts),dtype=float)
    for i,s in enumerate(starts): vals[i]=2.0*abs(np.dot(x[s:s+n],ref)/n)
    times=(starts+n/2)/fs
    return times,vals


def longest_run(mask,hop_s):
    best=run=0
    for v in mask:
        if bool(v): run+=1; best=max(best,run)
        else: run=0
    return best*hop_s


def metrics_for_channel(x,fs,f):
    times,env=envelope_frames(x,fs,f)
    if len(env)==0: return {"frames":0}
    db=20*np.log10(np.maximum(env,1e-15))
    ref=float(np.percentile(db,95))
    threshold=ref-12.0
    # Independent off-band demodulation is a noise-only proxy, not a production estimator.
    _,noise_env=envelope_frames(x,fs,f+40.0)
    noise_q95=float(np.percentile(20*np.log10(np.maximum(noise_env,1e-15)),95)) if len(noise_env) else None
    blocks={}
    for idx,(lo,hi) in enumerate(((0.0,1.0),(1.0,2.0)),1):
        sel=(times>=lo)&(times<hi)
        vals=db[sel]
        low=vals<threshold
        blocks[f"block_{idx}"]={
            "frames":int(len(vals)),
            "occupancy_above_global_q95_minus_12db":float(np.mean(~low)) if len(vals) else None,
            "low_envelope_max_run_ms":1000.0*longest_run(low,HOP_S),
            "envelope_q05_dbfs":finite(np.percentile(vals,5)) if len(vals) else None,
            "envelope_q50_dbfs":finite(np.percentile(vals,50)) if len(vals) else None,
        }
    return {"frames":int(len(env)),"global_q95_envelope_dbfs":ref,
            "offband_q95_envelope_dbfs":noise_q95,
            "pilot_to_offband_margin_db":finite(ref-noise_q95) if noise_q95 is not None else None,
            "candidate_each_block_occupancy_ge_0_97":all(
                b["occupancy_above_global_q95_minus_12db"] is not None and
                b["occupancy_above_global_q95_minus_12db"]>=0.97 for b in blocks.values()),
            "candidate_each_block_max_run_le_50ms":all(
                b["low_envelope_max_run_ms"]<=50.0 for b in blocks.values()),
            "blocks":blocks}


def run_case(cfg,i):
    level=float(cfg.get("pilot_dbfs",-72.0)); backend=SyntheticBackend(cfg,SEED_BASE+i)
    rig=Rig(backend,fs=FS,df=1.0,blocks=2,settle=SETTLE_S)
    row={"case":cfg["name"],"config":cfg,"rig_run":"not_run","clock_check":"not_run"}
    captured={}
    orig_run=rig.run
    def tracked_run(segs):
        try:
            wins,warns=orig_run(segs); captured["win"]=wins[0].copy(); row["rig_run"]="pass"; row["rig_warnings"]=list(warns); return wins,warns
        except Exception as e:
            row["rig_run"]="reject"; row["rig_error"]=f"{type(e).__name__}: {e}"; raise
    rig.run=tracked_run
    try:
        result=clock_check(rig,level_dbfs=level)
        row["clock_check"]="returned"
        row["production_clock_result"]={"ok":bool(result.get("ok")),"clock_ok":bool(result.get("clock_ok")),
          "signal_ok":bool(result.get("signal_ok")),"coherent":bool(result.get("coherent")),
          "channels":{ch:{k:finite(v.get(k)) for k in ("f0","ppm","level_dbfs","snr_db","coh_err_bins")}
                      for ch,v in result.get("ch",{}).items()}}
        pilot_f=float(np.mean([v["f0"] for v in result.get("ch",{}).values()]))
    except Exception as e:
        row["clock_check"]="raised"; row["clock_error"]=f"{type(e).__name__}: {e}"
        pilot_f=PILOT_HZ*(1.0+float(cfg.get("frequency_offset_ppm",0))*1e-6)
    win=captured.get("win")
    if win is None:
        # The production stability guard can reject before returning windows.
        # Reconstruct only the exact window sent to the guard from backend metadata.
        rec=backend.last_recording
        start=int(round((DELAY_S+SETTLE_S)*FS)); win=rec[start:start+int(WINDOW_S*FS)].copy()
    row["narrowband"]={ch:metrics_for_channel(win[:,c],FS,pilot_f) for c,ch in enumerate(("L","R"))}
    row["backend"] = backend.meta
    return row


def main():
    rows=[run_case(c,i) for i,c in enumerate(CASES)]
    out={"summary":{"task":"v2230 narrowband pilot occupancy diagnostics",
      "cases":len(rows),"fs_hz":FS,"window_s":WINDOW_S,"block_s":1.0,
      "demod_frame_ms":FRAME_S*1000,"demod_hop_ms":HOP_S*1000,
      "noise_dbfs_rms_sine_equiv":NOISE_DBFS,"noise_model":"broadband Gaussian, L/R rho=0.95",
      "pilot_levels_dbfs":list(PILOT_LEVELS),"leakage_L_dbfs":LEAKAGE_DBFS,
      "candidate_thresholds_are_not_approved":True,
      "interpretation":"The q95-12 dB threshold uses a global per-channel reference to avoid each silent block self-normalizing. The off-band demodulation is only a diagnostic noise proxy. The 0.97 occupancy / 50 ms run criteria remain unvalidated candidates."},
      "per_case":rows,"environment":{"python":platform.python_version(),"numpy":np.__version__,"scipy":scipy.__version__}}
    path=Path(__file__).with_name("v2230-narrowband-occupancy.json")
    path.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(out["summary"],ensure_ascii=False,indent=2),flush=True); print(f"JSON: {path}",flush=True)
if __name__=="__main__": main()