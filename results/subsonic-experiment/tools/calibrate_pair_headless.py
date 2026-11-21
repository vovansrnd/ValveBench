#!/usr/bin/env python3
"""Headless-калибровка пары ЦАП→АЦП: профиль АЧХ петли + шумовой пол АЦП.

Повторяет в точности обработчики GUI Setup (ui.py: calibrate_response,
calibrate_noise) теми же функциями valvebench и пишет
~/.config/valvebench/config.json в том же формате.

Использование:
  python3 tools/calibrate_pair_headless.py --dac hw:2,0 --adc hw:2,0 --rate 96000
"""
import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from valvebench import backend, calib, gainmatch, measure as m


def pair_key(dac_hw: str, adc_hw: str) -> str:
    dac_id = backend.stable_device_identity(dac_hw)
    adc_id = backend.stable_device_identity(adc_hw)
    if dac_id == adc_id:
        return dac_id
    digest = hashlib.sha256((dac_id + "\0" + adc_id).encode("utf-8")).hexdigest()[:24]
    return f"pair:{digest}"


def mixer_fp_pair(dac_hw: str, adc_hw: str, attempts: int = 6):
    """Fingerprint пары; при временно занятом USB amixer может таймаутить — повторяем."""
    if dac_hw == "sim" and adc_hw == "sim":
        return None
    def read(hw):
        if hw == "sim":
            return None
        for _ in range(attempts):
            fp = gainmatch.mixer_fingerprint_from_hw(hw)
            if fp is not None:
                return fp
            time.sleep(0.5)
        return None
    dac_fp, adc_fp = read(dac_hw), read(adc_hw)
    if (dac_hw != "sim" and dac_fp is None) or (adc_hw != "sim" and adc_fp is None):
        return None
    return dac_fp if dac_hw == adc_hw else {"dac": dac_fp, "adc": adc_fp}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dac", default="hw:2,0")
    ap.add_argument("--adc", default="hw:2,0")
    ap.add_argument("--rate", type=int, default=96000)
    args = ap.parse_args()

    cal = calib.load()
    dac_hw, adc_hw, fs = args.dac, args.adc, int(args.rate)
    key = pair_key(dac_hw, adc_hw)
    pad_db = float(cal.get("adc_att_db", 0.0) or 0.0)
    print(f"пара: {dac_hw} → {adc_hw}, fs={fs}, ключ={key!r}, PAD={pad_db:.2f} дБ")

    before_fp = mixer_fp_pair(dac_hw, adc_hw)
    if before_fp is None:
        print("ОШИБКА: не удалось прочитать состояние микшеров; профиль не сохраняется", file=sys.stderr)
        return 2

    be = backend.AlsaCli(hw_out=dac_hw, hw_in=adc_hw,
                         in_map=cal["in_map"], out_map=cal["out_map"])
    rig = m.Rig(be, cal, fs=fs, blocks=2)
    rig.open()
    # --- 1. АЧХ петли (плоский свип) — как в GUI calibrate_response ---
    print("1/2: плоский свип петли 12 с, уровень 1 кГц −12 dBFS…", flush=True)
    res = m.sweep(rig, f_lo=20.0, f_hi=min(20000.0, 0.40 * fs), dur=12.0,
                  level_1k_dbfs=-12.0, mode="flat", iec=False)
    after_fp = mixer_fp_pair(dac_hw, adc_hw)
    if after_fp is None:
        print("ОШИБКА: не удалось прочитать состояние микшеров после свипа; профиль не сохранён",
              file=sys.stderr)
        return 2
    if before_fp != after_fp:
        print("ОШИБКА: настройки микшеров изменились во время калибровки; профиль не сохранён",
              file=sys.stderr)
        return 2
    profile = m.build_response_calibration(res, key, after_fp, fs=fs)
    profile["mixer_fingerprint"] = after_fp
    profile["adc_att_db"] = pad_db
    cal["frequency_response_profiles"][str(fs)] = profile
    calib.save(cal)
    for name in ("L", "R"):
        ch = profile["channels"][name]
        f = np.asarray(ch["frequency_hz"])
        fr = np.asarray(ch["fr_norm_db"])
        print(f"  {name}: точек {len(f)}, полоса {f[0]:.0f}–{f[-1]:.0f} Гц, "
              f"АЧХ в полосе {fr.min():+.2f}…{fr.max():+.2f} дБ (норм. на 1 кГц)")
    print("  ПРОФИЛЬ АЧХ СОХРАНЁН: frequency_response_profiles[%s]" % fs)

    # --- 2. Шумовой пол АЦП — как в GUI calibrate_noise ---
    print("2/2: шумовой пол АЦП, 12 с тишины…", flush=True)
    res = m.noise(rig, seconds=12.0)
    after_fp = mixer_fp_pair(dac_hw, adc_hw)
    if after_fp is None:
        print("ОШИБКА: не удалось прочитать состояние микшеров после записи; профиль не сохранён",
              file=sys.stderr)
        return 2
    if before_fp != after_fp:
        print("ОШИБКА: микшер изменился во время записи; шумовой профиль не сохранён", file=sys.stderr)
        return 2
    if any("ПЕРЕГРУЗКА" in str(w).upper() for w in res.get("warnings", [])):
        print("ОШИБКА: АЦП перегружен во время измерения шума; профиль не сохранён", file=sys.stderr)
        return 2
    profile = {
        "kind": "adc_noise_floor", "schema": 1, "device_key": key,
        "fs": int(fs), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mixer_fingerprint": after_fp,
        "frequency_hz": np.asarray(res["ch"]["L"]["freq"], dtype=float).tolist(),
        "channels": {ch: {"density_dbfs_sqrt_hz":
                          np.asarray(res["ch"][ch]["dens_dbfs"], dtype=float).tolist()}
                     for ch in ("L", "R")},
    }
    cal["noise_floor_profiles"][str(fs)] = profile
    calib.save(cal)
    print("  ПРОФИЛЬ ШУМА СОХРАНЁН: noise_floor_profiles[%s]" % fs)
    print(m.text_noise(res))
    if res.get("warnings"):
        print("предупреждения:", "; ".join(res["warnings"]))
    print("КАЛИБРОВКА ЗАВЕРШЕНА:", calib.PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
