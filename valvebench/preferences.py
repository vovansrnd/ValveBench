"""Persistent UI preferences, deliberately separate from measurement calibration."""
from __future__ import annotations

import json
import os
import hashlib
import tempfile

PATH = os.path.expanduser("~/.config/valvebench/ui.json")


def load(path: str | None = None) -> dict:
    path = path or PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save(data: dict, path: str | None = None) -> None:
    path = path or PATH
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ui-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pref_name(title: str) -> str:
    return "pref_" + hashlib.sha1(str(title).encode("utf-8")).hexdigest()[:12]


def migrate_0_1_16_defaults(data: dict) -> dict:
    """One-time migration of old application defaults; preserve user choices.

    0.1.15 used RIAA deviation and very low generator/noise levels as defaults.
    Those values are moved only when the saved value is exactly the old default.
    Any other value is treated as an intentional user selection and preserved.
    """
    out = dict(data or {})
    flag = "_migration_0_1_16_measurement_defaults"
    if out.get(flag):
        return out

    def migrate(page: str, title: str, old, new):
        key = f"{page}/{_pref_name(title)}"
        value = out.get(key, None)
        if value is not None:
            try:
                if abs(float(value) - float(old)) < 1e-9:
                    out[key] = new
            except (TypeError, ValueError):
                pass

    # RIAA-view index 0 was the previous default. Normalized ordinary response
    # is the neutral/default view; a user's other saved selection is preserved.
    migrate("resp", "Показывать", 0, 1)
    migrate("noise_response", "График", 0, 1)

    # Replace the old conservative defaults but retain any user-selected value.
    migrate("live", "Уровень генератора, dBFS", -20, -6)
    migrate("thd", "Уровень генератора, dBFS", -20, -6)
    migrate("live", "Уровень теста, dBFS", -30, -12)

    # 0.1.15 used shorter field titles. Transfer their values into the new
    # clearer controls, changing only the old conservative defaults.
    legacy_rows = (
        ("resp", "Уровень @1 кГц, dBFS", "Уровень генератора @1 кГц, dBFS", -20.0, -6.0),
        ("imd", "Пик суммы, dBFS", "Пиковый уровень суммы двух тонов, dBFS", -12.0, -6.0),
    )
    for page, old_title, new_title, old_default, new_default in legacy_rows:
        old_key = f"{page}/{_pref_name(old_title)}"
        new_key = f"{page}/{_pref_name(new_title)}"
        if old_key in out and new_key not in out:
            try:
                value = float(out[old_key])
                out[new_key] = new_default if abs(value - old_default) < 1e-9 else value
            except (TypeError, ValueError):
                pass

    # The old ResponsePage default was inverse-RIAA at index 0; the new default
    # is a flat sweep at index 0. Both the old inverse default and old flat choice
    # map to flat in the new neutral workflow. The special test remains opt-in.
    old_mode_key = f"resp/{_pref_name('Генератор')}"
    new_mode_key = f"resp/{_pref_name('Тестовый сигнал')}"
    if old_mode_key in out and new_mode_key not in out:
        out[new_mode_key] = 0

    # Spectrum FFT was historically 8k and was not persisted in 0.1.15.
    # In case a local build had already stored it, migrate only that old default.
    fft_key = "spectrum/pref_spectrum_fft"
    if out.get(fft_key) == 0:
        out[fft_key] = 5

    # The old noise level row capped at -12 and started at -42 RMS. The new row
    # has a different label; carry the user's non-default choice forward.
    old_noise_key = f"noise_response/{_pref_name('Цифровой RMS-уровень, dBFS')}"
    new_noise_key = f"noise_response/{_pref_name('Желаемый RMS-уровень шума, dBFS')}"
    if old_noise_key in out and new_noise_key not in out:
        try:
            old_value = float(out[old_noise_key])
            out[new_noise_key] = -12.0 if abs(old_value - (-42.0)) < 1e-9 else min(-3.0, max(-60.0, old_value))
        except (TypeError, ValueError):
            pass

    out[flag] = True
    return out
