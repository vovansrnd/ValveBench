"""Независимая калибровка ЦАП/АЦП, сохранённая отдельно для каждой физической карты."""
from __future__ import annotations

import json
import math
import os

PATH = os.path.expanduser("~/.config/valvebench/config.json")

DEFAULTS = {
    "device": "sim",
    "device_key": "sim",
    "fs": 96000,
    "in_map": [0, 1],
    "out_map": [0, 1],
    "out_fs_vrms": [None, None],
    "in_fs_vrms": [None, None],
    "att_db": 0.0,
    "adc_att_db": 0.0,
    "mains": 50.0,
    "band_hi": 20000.0,
    "frequency_response_profiles": {},
    "noise_floor_profiles": {},
    "auto_apply_response": True,
    "auto_show_noise_floor": True,
}

PROFILE_KEYS = (
    "in_map", "out_map", "out_fs_vrms", "in_fs_vrms", "att_db", "adc_att_db", "mains", "band_hi",
    "frequency_response_profiles", "noise_floor_profiles",
)


def response_profile_pad_matches(profile, adc_att_db, tolerance_db=0.01):
    """True only when an FR calibration used the currently configured ADC pad."""
    if not isinstance(profile, dict):
        return False
    try:
        expected = float(profile.get("adc_att_db", 0.0) or 0.0)
        actual = float(adc_att_db or 0.0)
        return math.isfinite(expected) and math.isfinite(actual) and abs(expected - actual) <= tolerance_db
    except (TypeError, ValueError):
        return False


def _copy_value(value):
    if isinstance(value, dict):
        return {str(k): _copy_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_value(v) for v in value]
    if isinstance(value, tuple):
        return [_copy_value(v) for v in value]
    return value


class Calibration:
    def __init__(self, data=None):
        data = data or {}
        self.d = {k: _copy_value(v) for k, v in DEFAULTS.items()}
        for key in DEFAULTS:
            if key in data:
                value = data[key]
                if key == "out_fs_vrms" and isinstance(value, (int, float)):
                    value = [float(value), float(value)]
                self.d[key] = _copy_value(value)
        self._profiles = _copy_value(data.get("device_profiles", {}))
        self._active_profile_key = str(data.get("active_device_key") or self.d.get("device_key") or "sim")
        for key in ("out_fs_vrms", "in_fs_vrms"):
            value = self.d.get(key)
            if not isinstance(value, (list, tuple)):
                self.d[key] = [None, None]
            else:
                vals = list(value[:2])
                while len(vals) < 2:
                    vals.append(None)
                self.d[key] = vals
        if not isinstance(self.d.get("frequency_response_profiles"), dict):
            self.d["frequency_response_profiles"] = {}
        if not isinstance(self.d.get("noise_floor_profiles"), dict):
            self.d["noise_floor_profiles"] = {}
        # Load a saved per-device profile, if the persisted active key is known.
        saved = self._profiles.get(self._active_profile_key)
        if isinstance(saved, dict):
            self._apply_profile(saved)

    def _profile_snapshot(self):
        return {key: _copy_value(self.d[key]) for key in PROFILE_KEYS}

    def _apply_profile(self, profile):
        for key in PROFILE_KEYS:
            if key in profile:
                self.d[key] = _copy_value(profile[key])

    def activate_profile(self, device_key, device_hw=None):
        """Switch active per-card calibration; cards never share voltage/FR data."""
        key = str(device_key or "sim")
        old_key = self._active_profile_key
        if old_key:
            self._profiles[old_key] = self._profile_snapshot()
        if key in self._profiles and isinstance(self._profiles[key], dict):
            self._apply_profile(self._profiles[key])
        else:
            # A legacy config without a profile belongs to its previously selected
            # ALSA endpoint only. Migrate it once; do not copy it to other cards.
            same_legacy_endpoint = (not self._profiles and
                                    str(self.d.get("device", "sim")) == str(device_hw or "sim"))
            if not same_legacy_endpoint:
                fresh = {k: _copy_value(DEFAULTS[k]) for k in PROFILE_KEYS}
                self._apply_profile(fresh)
            self._profiles[key] = self._profile_snapshot()
        self._active_profile_key = key
        self.d["device_key"] = key
        if device_hw is not None:
            self.d["device"] = str(device_hw)

    def serialize(self):
        self._profiles[self._active_profile_key] = self._profile_snapshot()
        data = _copy_value(self.d)
        data["device_profiles"] = _copy_value(self._profiles)
        data["active_device_key"] = self._active_profile_key
        return data

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v

    def get(self, k, default=None):
        return self.d.get(k, default)

    def out_full_scale_vrms(self, ch):
        return self.d["out_fs_vrms"][int(ch)]

    def out_v(self, dbfs, ch=0):
        v = self.out_full_scale_vrms(ch)
        return None if v is None else float(v) * 10 ** (float(dbfs) / 20)

    def dut_in_v(self, dbfs, ch=0):
        v = self.out_v(dbfs, ch)
        return None if v is None else v * 10 ** (float(self.d["att_db"]) / 20)

    def in_v(self, dbfs, ch):
        """DUT output voltage inferred from ADC level, correcting a DUT→ADC pad.

        ``in_fs_vrms`` represents the ADC full-scale voltage at its input
        connector, established by a loopback calibration with the configured
        external pad physically in-line. ``adc_att_db`` is the (negative) gain
        of that DUT→ADC pad. The reported voltage is referred back to the DUT
        output, before the pad.
        """
        v = self.d["in_fs_vrms"][int(ch)]
        if v is None:
            return None
        adc_v = float(v) * 10 ** (float(dbfs) / 20)
        pad_db = float(self.d.get("adc_att_db", 0.0) or 0.0)
        return adc_v * 10 ** (-pad_db / 20)

    def gain_db(self, gen_dbfs, rec_dbfs, ch):
        vi, vo = self.dut_in_v(gen_dbfs, ch), self.in_v(rec_dbfs, ch)
        if vi is None or vo is None or vi <= 0 or vo <= 0:
            return None
        return 20 * math.log10(vo / vi)

    def set_out_from_meter(self, gen_dbfs, vrms, ch=0):
        self.d["out_fs_vrms"][int(ch)] = float(vrms) / 10 ** (float(gen_dbfs) / 20)

    def set_in_from_loop(self, gen_dbfs, rec_dbfs, ch):
        """Calibrate ADC full scale through the configured DUT→ADC pad.

        The loopback must include the same pad used for DUT measurements.
        ``in_fs_vrms`` remains the voltage at the ADC connector; ``in_v`` then
        refers it back to the DUT side using ``adc_att_db``.
        """
        v = self.out_v(gen_dbfs, ch)
        if v is None:
            raise ValueError(f"сначала откалибруй выход ЦАП {('L','R')[int(ch)]}")
        pad_gain = 10 ** (float(self.d.get("adc_att_db", 0.0) or 0.0) / 20)
        adc_v = v * pad_gain
        self.d["in_fs_vrms"][int(ch)] = adc_v / 10 ** (float(rec_dbfs) / 20)


def fmt_v(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1:
        return f"{v:.3f} В"
    if a >= 1e-3:
        return f"{v * 1e3:.3f} мВ"
    return f"{v * 1e6:.2f} мкВ"


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return Calibration(json.load(f))
    except (OSError, ValueError, TypeError):
        return Calibration()


def save(cal):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cal.serialize(), f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, PATH)
