#!/usr/bin/env python3
"""v2.23.0 — independent-window validation of local-PSD frequency uncertainty.

Each row is a fresh, non-overlapping noise realization (not a sliding window
or a repeated phase over the same noise). Uses the frozen LSQ frequency fit;
compares absolute frequency error with the white-noise CRLB formula using the
known injected-noise local PSD, and reports L/R error correlation. Diagnostic
only: no sigma threshold or production estimator change is proposed.
"""
from __future__ import annotations
import json, math, platform, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import scipy
from scipy.signal import welch
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(HERE))
from v2188_estimators import est_lsq

FS=96000.0; DURATIONS=(2.0,4.0,6.0); COLORS=("white","colored")
PILOT_NOM=1000.0; PILOT_TRUE=PILOT_NOM*(1-23.5e-6)
PILOT_DBFS=-72.0; NOISE_DBFS=-80.0; TRIALS=32; SEED0=223300

def amp(db): return 10**(float(db)/20)
def finite(x):
 try:
  z=float(x); return z if math.isfinite(z) else None
 except (TypeError,ValueError): return None

def make_noise(n,seed,color):
 rng=np.random.default_rng(seed); a=rng.standard_normal(n); b=rng.standard_normal(n)
 if color=="colored":
  f=np.fft.rfftfreq(n,1/FS)
  for z in (a,b):
   Z=np.fft.rfft(z); Z/=np.sqrt(1+(f/1400.0)**2); z[:]=np.fft.irfft(Z,n=n)
 b=0.95*a+math.sqrt(1-.95**2)*b
 pair=np.column_stack((a,b))
 rms=float(np.sqrt(np.mean(pair*pair)))
 return pair*(amp(NOISE_DBFS)/math.sqrt(2))/max(rms,1e-30)

def local_psd(noise):
 nperseg=min(65536,len(noise))
 f,p=welch(noise,fs=FS,nperseg=nperseg,noverlap=nperseg//2,scaling="density")
 keep=(np.abs(f-PILOT_TRUE)>=2.0)&(np.abs(f-PILOT_TRUE)<=32.0)
 if not np.any(keep): raise RuntimeError("local PSD band has no bins")
 return float(np.median(p[keep])),float(np.mean(p[keep])),float(f[1]-f[0])

def fit(x):
 r=est_lsq(x,FS,PILOT_NOM)
 f=finite(r.get("f_est")); var=finite(r.get("variance_hz2"))
 return {"f_est_hz":f,"fit_error_ppm":(f-PILOT_TRUE)/PILOT_TRUE*1e6 if f is not None else None,
         "status":r.get("status"),"estimator_sigma_ppm":math.sqrt(var)/PILOT_TRUE*1e6 if var is not None and var>=0 else None,
         "snr_carrier_db":finite(r.get("snr_carrier_db"))}

def main():
 rows=[]
 for duration in DURATIONS:
  n=int(round(duration*FS)); t=np.arange(n)/FS
  pilot=amp(PILOT_DBFS)*np.sin(2*np.pi*PILOT_TRUE*t+0.27)
  for color in COLORS:
   for trial in range(TRIALS):
    seed=SEED0+int(duration*10)*10000+(0 if color=="white" else 5000)+trial
    noise=make_noise(n,seed,color)
    for ch,name in ((0,"L"),(1,"R")):
     psd_med,psd_mean,df=local_psd(noise[:,ch])
     # N0 is the one-sided PSD; A is sinusoidal peak amplitude.
     sigma=math.sqrt(3.0*psd_med/(math.pi**2*amp(PILOT_DBFS)**2*duration**3))
     result=fit(pilot+noise[:,ch])
     err=abs(result["fit_error_ppm"])*1e-6*PILOT_TRUE if result["fit_error_ppm"] is not None else None
     rows.append({"duration_s":duration,"noise_color":color,"trial":trial,"seed":seed,
       "channel":name,"noise_rms_dbfs":NOISE_DBFS,"local_psd_median_v2_per_hz":psd_med,
       "local_psd_mean_v2_per_hz":psd_mean,"psd_bin_width_hz":df,"crlb_sigma_hz":sigma,
       "crlb_sigma_ppm":sigma/PILOT_TRUE*1e6,
       "abs_error_over_crlb":err/sigma if err is not None else None,**result})
 groups=defaultdict(list)
 for r in rows: groups[(r["duration_s"],r["noise_color"])].append(r)
 summary={}
 for (duration,color),g in sorted(groups.items()):
  vals=[r for r in g if r["fit_error_ppm"] is not None]
  z=[r["abs_error_over_crlb"] for r in vals if r["abs_error_over_crlb"] is not None]
  cherr={name:[r["fit_error_ppm"] for r in vals if r["channel"]==name] for name in ("L","R")}
  corr=float(np.corrcoef(cherr["L"],cherr["R"])[0,1]) if len(cherr["L"])>2 else None
  summary[f"{duration:g}s {color}"]={"independent_records":TRIALS,"channel_rows":len(vals),
   "abs_error_over_crlb_median":float(np.median(z)) if z else None,
   "abs_error_over_crlb_p90":float(np.percentile(z,90)) if z else None,
   "abs_error_over_crlb_max":max(z) if z else None,
   "signed_error_mean_ppm":float(np.mean([r["fit_error_ppm"] for r in vals])) if vals else None,
   "signed_error_std_ppm":float(np.std([r["fit_error_ppm"] for r in vals],ddof=1)) if len(vals)>1 else None,
   "L_R_error_correlation":corr,
   "estimator_status_counts":{s:sum(r["status"]==s for r in vals) for s in sorted({r["status"] for r in vals})}}
 out={"summary":{"task":"v2230 independent local-PSD uncertainty validation","fs_hz":FS,
   "pilot_nominal_hz":PILOT_NOM,"pilot_true_hz":PILOT_TRUE,"pilot_level_dbfs":PILOT_DBFS,
   "broadband_noise_rms_dbfs":NOISE_DBFS,"durations_s":list(DURATIONS),"noise_models":list(COLORS),
   "independent_trials_per_duration_and_noise_model":TRIALS,
   "crlb":"sigma_f = sqrt(3*N0/(pi^2*A^2*T^3)); N0 is measured one-sided local PSD, A is peak amplitude",
   "summary_by_cell":summary,"interpretation":"Noise records are independently regenerated per trial and do not overlap. L/R noise is intentionally correlated (rho=0.95 before normalization). This is a first validation sample, not a calibrated production confidence bound."},
   "per_trial":rows,"environment":{"python":platform.python_version(),"numpy":np.__version__,"scipy":scipy.__version__}}
 path=HERE/"v2230-independent-psd-uncertainty.json"; path.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 print(json.dumps(out["summary"],ensure_ascii=False,indent=2),flush=True); print(f"JSON: {path}",flush=True)
if __name__=="__main__": main()