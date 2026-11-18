#!/usr/bin/env python3
"""v2.23.0 — quantify 1 kHz leakage bias and alternative pilot frequencies.

Uses frozen v2188 est_lsq unchanged. Sweeps nominal pilots 1000/997/1013 Hz,
2/4/6 s records, an independently clock-shifted pilot (-23.5 ppm), the fixed
1000 Hz leakage measured in silence (L -109 dBFS, R -125 dBFS) and stress
levels, phase, realistic broadband noise and a 60 Hz comb. AM/FM controls are
reported separately. Nearby/line flags remain diagnostics, never hard gates.
"""
from __future__ import annotations
import json, math, platform, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import scipy

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(HERE))
from v2188_estimators import est_lsq

FS=96000.0
PILOT_NOMINALS=(1000.0,997.0,1013.0)
DURATIONS=(2.0,4.0,6.0)
PILOT_DBFS=-72.0
NOISE_DBFS=-80.0
LEAKAGE_DBFS=(-109.0,-100.0,-90.0)
PHASE_TRIALS=8
SEED0=223200


def amp(db): return 10.0**(float(db)/20.0)
def finite(x):
    try:
        v=float(x); return v if math.isfinite(v) else None
    except (TypeError,ValueError): return None

def noise_pair(n,seed):
    rng=np.random.default_rng(seed)
    # Broadband + gently coloured component; exactly normalized to -80 dBFS RMS sine-equivalent.
    white=rng.standard_normal((n,2))
    colored=rng.standard_normal((n,2))
    # Low-pass the coloured component in the frequency domain (1/f-like tilt).
    f=np.fft.rfftfreq(n,1/FS)
    for c in (0,1):
        X=np.fft.rfft(colored[:,c]); X/=np.sqrt(1.0+(f/1200.0)**2); colored[:,c]=np.fft.irfft(X,n=n)
    z=0.65*white+0.35*colored
    z[:,1]=0.95*z[:,0]+math.sqrt(1-0.95**2)*z[:,1]
    rms=np.sqrt(np.mean(z*z))
    return z*(amp(NOISE_DBFS)/math.sqrt(2.0))/max(rms,1e-30)

def hum_comb(t):
    # Observed 60 Hz comb; amplitudes deliberately weak compared with broadband RMS.
    out=np.zeros_like(t)
    for k,db in enumerate((-88,-91,-96,-101,-106,-110,-114,-118),1):
        out += amp(db)*np.sin(2*np.pi*(60.0*k)*t+0.17*k)
    return out

def synth(n,f_nom,seed,channel,leak_dbfs=None,leak_phase=0.0,mode="steady",mod_depth=0.0,mod_hz=0.0,dev_hz=0.0):
    t=np.arange(n)/FS
    f_true=f_nom*(1.0-23.5e-6)
    phase=2*np.pi*((seed*37)%997)/997.0
    phase_offset=phase
    if mode=="am":
        envelope=1.0+mod_depth*np.sin(2*np.pi*mod_hz*t)
        pilot=amp(PILOT_DBFS)*envelope*np.sin(2*np.pi*f_true*t+phase_offset)
    elif mode=="fm":
        inst=f_true+dev_hz*np.sin(2*np.pi*mod_hz*t)
        ph=phase_offset+2*np.pi*np.cumsum(inst)/FS
        pilot=amp(PILOT_DBFS)*np.sin(ph)
    else:
        pilot=amp(PILOT_DBFS)*np.sin(2*np.pi*f_true*t+phase_offset)
    x=pilot+noise_pair(n,seed)[:,channel]+hum_comb(t)
    if leak_dbfs is not None:
        actual_leak=leak_dbfs if channel==0 else min(leak_dbfs,-125.0 if leak_dbfs==-109.0 else leak_dbfs)
        x += amp(actual_leak)*np.sin(2*np.pi*1000.0*t+leak_phase)
    return x,f_true

def fit_record(x,f_nom):
    r=est_lsq(x,FS,f_nom)
    f=finite(r.get("f_est"))
    return {"f_est_hz":f,"fit_error_ppm":(f-f_nom*(1-23.5e-6))/(f_nom*(1-23.5e-6))*1e6 if f is not None else None,
            "status":r.get("status"),"snr_carrier_db":finite(r.get("snr_carrier_db")),
            "line_hz":finite(r.get("line_hz")),"line_dbc":finite(r.get("line_dbc")),
            "nearby_tone_hz":finite(r.get("nearby_tone_hz")),"nearby_tone_db":finite(r.get("nearby_tone_db"))}

def main():
    rows=[]; baselines={}; trial_id=0
    # Paired baseline and leakage rows; each seed/phase has identical noise and pilot.
    for f_nom in PILOT_NOMINALS:
      durations=(2.0,4.0,6.0) if f_nom==1000.0 else (2.0,6.0)
      for duration in durations:
       n=int(round(FS*duration))
       for channel,chname in ((0,"L"),(1,"R")):
        for trial in range(PHASE_TRIALS):
         seed=SEED0+int(f_nom)*1000+int(duration*10)*100+trial
         x,f_true=synth(n,f_nom,seed,channel)
         base=fit_record(x,f_nom)
         baselines[(f_nom,duration,chname,trial)]=base
         rows.append({"trial":trial_id,"kind":"pilot_only_baseline","pilot_nominal_hz":f_nom,
           "pilot_true_hz":f_true,"duration_s":duration,"channel":chname,"trial_index":trial,
           "seed":seed,**base}); trial_id+=1
         for leak_db in LEAKAGE_DBFS:
          rng=np.random.default_rng(seed+int(abs(leak_db)*100))
          relative_phase=float(rng.uniform(0,2*np.pi))
          actual_leak_db = leak_db if channel == 0 else (-125.0 if leak_db == -109.0 else leak_db)
          x,_=synth(n,f_nom,seed,channel,leak_db,relative_phase)
          fit=fit_record(x,f_nom); b=base.get("f_est_hz"); f=fit.get("f_est_hz")
          induced=((f-b)/f_true*1e6) if f is not None and b is not None else None
          rows.append({"trial":trial_id,"kind":"1000hz_leakage","pilot_nominal_hz":f_nom,
            "pilot_true_hz":f_true,"leakage_hz":1000.0,"leakage_dbfs":leak_db,
            "actual_leakage_dbfs":actual_leak_db,
            "leakage_dbc_vs_pilot":actual_leak_db-PILOT_DBFS,"relative_phase_rad":relative_phase,
            "duration_s":duration,"channel":chname,"trial_index":trial,"seed":seed,
            "baseline_f_est_hz":b,"induced_shift_ppm":induced,**fit}); trial_id+=1
    # AM/FM controls at 1 kHz: sidebands are not automatically grounds for rejection.
    for channel,chname in ((0,"L"),(1,"R")):
      for mode,depth,hz,dev in (("am",0.2,5.0,0.0),("am",0.5,5.0,0.0),
                                ("am",0.5,20.0,0.0),("fm",0.0,5.0,0.05),
                                ("fm",0.0,5.0,0.10),("fm",0.0,20.0,0.05)):
       for trial in range(PHASE_TRIALS):
        seed=SEED0+900000+channel*10000+int(hz*100)+trial
        n=int(2*FS); x,f_true=synth(n,1000.0,seed,channel,mode=mode,mod_depth=depth,mod_hz=hz,dev_hz=dev)
        fit=fit_record(x,1000.0)
        rows.append({"trial":trial_id,"kind":f"{mode}_control","mode":mode,"depth":depth,
          "modulation_hz":hz,"fm_deviation_hz":dev,"pilot_nominal_hz":1000.0,
          "pilot_true_hz":f_true,"duration_s":2.0,"channel":chname,"trial_index":trial,
          "seed":seed,**fit}); trial_id+=1

    grouped=defaultdict(list)
    for r in rows:
      if r["kind"]=="1000hz_leakage": grouped[(r["pilot_nominal_hz"],r["duration_s"],r["channel"],r["leakage_dbfs"])].append(r)
    summaries={}
    for key,items in sorted(grouped.items()):
      shifts=[abs(r["induced_shift_ppm"]) for r in items if r["induced_shift_ppm"] is not None]
      errors=[abs(r["fit_error_ppm"]) for r in items if r["fit_error_ppm"] is not None]
      summaries[f"pilot={key[0]:g}Hz T={key[1]:g}s {key[2]} nominal_leak={key[3]:g}dBFS"]={
       "nominal_leakage_control_dbfs":key[3],
       "actual_leakage_dbfs":items[0]["actual_leakage_dbfs"],
       "n":len(items),"induced_abs_median_ppm":float(np.median(shifts)) if shifts else None,
       "induced_abs_p90_ppm":float(np.percentile(shifts,90)) if shifts else None,
       "induced_abs_max_ppm":max(shifts) if shifts else None,
       "absolute_error_p90_ppm":float(np.percentile(errors,90)) if errors else None,
       "status_counts":{s:sum(r["status"]==s for r in items) for s in sorted({r["status"] for r in items})},
       "nearby_probe_hits":sum(r["nearby_tone_hz"] is not None for r in items),
       "line_qc_hits":sum(r["line_hz"] is not None for r in items)}
    controls={}
    for mode in ("am_control","fm_control"):
      g=[r for r in rows if r["kind"]==mode]
      controls[mode]={"n":len(g),"nearby_hits":sum(r["nearby_tone_hz"] is not None for r in g),
        "line_hits":sum(r["line_hz"] is not None for r in g),
        "fit_error_abs_p90_ppm":float(np.percentile([abs(r["fit_error_ppm"]) for r in g if r["fit_error_ppm"] is not None],90)) if g else None,
        "status_counts":{s:sum(r["status"]==s for r in g) for s in sorted({r["status"] for r in g})}}
    out={"summary":{"task":"v2230 1 kHz leakage and pilot-frequency bias",
      "fs_hz":FS,"pilot_nominals_hz":list(PILOT_NOMINALS),"durations_s":[2,4,6],
      "pilot_level_dbfs":PILOT_DBFS,"broadband_noise_dbfs":NOISE_DBFS,
      "observed_leakage_L_dbfs":-109.0,"observed_leakage_R_dbfs":-125.0,
      "per_channel_actual_leakage_level_is_recorded":True,
      "stress_leakage_dbfs":list(LEAKAGE_DBFS),"clock_offset_ppm":-23.5,
      "comb":"60 Hz harmonics, low-level synthetic controls",
      "nearby_hard_gate":False,"production_changes":False,
      "leakage_cells":summaries,"modulation_controls":controls,
      "interpretation":"Compare induced shifts against paired no-leak baselines. A line/nearby detector hit is recorded but never treated as rejection. Alternative pilot candidates are diagnostics only; no production pilot frequency is changed."},
      "per_trial":rows,"environment":{"python":platform.python_version(),"numpy":np.__version__,"scipy":scipy.__version__}}
    path=HERE/"v2230-pilot-leakage-bias.json"
    path.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(out["summary"],ensure_ascii=False,indent=2),flush=True); print(f"JSON: {path}",flush=True)
if __name__=="__main__": main()