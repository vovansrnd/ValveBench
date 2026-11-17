#!/usr/bin/env python3
"""Compare ValveBench noise-response CSVs to characterize a subsonic high-pass.

The reference file is the same DUT/measurement chain with the filter bypassed.
Each following file is a filtered capture made with the same ValveBench settings.
The difference of RIAA-deviation curves cancels the common RIAA target and the
common DAC/ADC response, leaving the incremental filter response.

Example:
  ./run.sh cli --rate 96000 noise-response --kind riaa --seconds 60 \
      --level -36 --lo 2 --hi 1000 --csv measurements/subsonic/bypass.csv
  # Enable one filter position, then repeat to .../10Hz.csv
  python tools/analyze_subsonic_filter.py \
      measurements/subsonic/bypass.csv measurements/subsonic/10Hz.csv

The input files are ValveBench's CSV files produced by 'noise-response --csv'.
Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = (
    "freq_hz",
    "L_RIAA_dev_db",
    "R_RIAA_dev_db",
    "L_coherence",
    "R_coherence",
)
REPORT_FREQUENCIES_HZ = (
    2.0, 3.0, 5.0, 8.0, 10.0, 12.5, 15.0, 20.0,
    25.0, 31.5, 40.0, 50.0, 80.0, 100.0, 200.0, 500.0, 1000.0,
)


def _finite_float(value: str, label: str, line: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"строка {line}: поле {label!r} не является числом: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"строка {line}: поле {label!r} не конечно")
    return number


def read_valvebench_csv(path: Path) -> dict[str, list[tuple[float, float, float]]]:
    """Return per-channel sorted (frequency, RIAA deviation, coherence) rows."""
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValueError(f"не удалось открыть {path}: {exc}") from exc

    channels: dict[str, list[tuple[float, float, float]]] = {"L": [], "R": []}
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: пустой CSV")
        missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"{path}: это не CSV ValveBench noise-response; отсутствуют столбцы: "
                + ", ".join(missing)
            )
        for line, row in enumerate(reader, start=2):
            if not row or not any((value or "").strip() for value in row.values()):
                continue
            freq = _finite_float(row["freq_hz"], "freq_hz", line)
            if freq <= 0:
                raise ValueError(f"{path}, строка {line}: частота должна быть > 0")
            for channel in ("L", "R"):
                db = _finite_float(row[f"{channel}_RIAA_dev_db"], f"{channel}_RIAA_dev_db", line)
                coh = _finite_float(row[f"{channel}_coherence"], f"{channel}_coherence", line)
                if not 0.0 <= coh <= 1.000001:
                    raise ValueError(f"{path}, строка {line}: coherence вне [0, 1]: {coh}")
                channels[channel].append((freq, db, min(1.0, coh)))

    for channel, rows in channels.items():
        rows.sort(key=lambda item: item[0])
        if len(rows) < 8:
            raise ValueError(f"{path}: слишком мало точек для канала {channel}: {len(rows)}")
        for previous, current in zip(rows, rows[1:]):
            if current[0] <= previous[0]:
                raise ValueError(f"{path}: повторяющиеся частоты в канале {channel}")
        if rows[0][0] > 5.0 or rows[-1][0] < 990.0:
            raise ValueError(
                f"{path}: диапазон {rows[0][0]:.2f}…{rows[-1][0]:.1f} Гц; "
                "для данного отчёта нужен захват примерно от 2–5 Гц и почти до 1 кГц"
            )
    return channels


def interpolate(rows: list[tuple[float, float, float]], frequency: float) -> tuple[float, float] | None:
    """Interpolate dB and coherence on a logarithmic frequency axis."""
    if frequency < rows[0][0] or frequency > rows[-1][0]:
        return None
    lo, hi = 0, len(rows) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if rows[mid][0] < frequency:
            lo = mid
        else:
            hi = mid
    f0, d0, c0 = rows[lo]
    f1, d1, c1 = rows[hi]
    if abs(frequency - f0) <= max(1e-12, frequency * 1e-12):
        return d0, c0
    if abs(frequency - f1) <= max(1e-12, frequency * 1e-12):
        return d1, c1
    ratio = math.log(frequency / f0) / math.log(f1 / f0)
    return d0 + ratio * (d1 - d0), c0 + ratio * (c1 - c0)


def first_order_db(frequency: float, corner_hz: float) -> float:
    """Magnitude response of a first-order RC high-pass, normalized at HF."""
    return -10.0 * math.log10(1.0 + (corner_hz / frequency) ** 2)


def fit_first_order(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Fit fc by deterministic log-grid search; return (fc, RMS residual dB)."""
    if len(points) < 8:
        return None

    def score(log_fc: float) -> float:
        fc = math.exp(log_fc)
        return sum((measured - first_order_db(freq, fc)) ** 2
                   for freq, measured in points) / len(points)

    low, high = math.log(0.05), math.log(1000.0)
    best_x = low
    best_score = float("inf")
    # Coarse search, followed by a local golden-section refinement in log(fc).
    steps = 500
    step = (high - low) / steps
    for index in range(steps + 1):
        candidate = low + index * step
        candidate_score = score(candidate)
        if candidate_score < best_score:
            best_x, best_score = candidate, candidate_score

    left, right = max(low, best_x - step), min(high, best_x + step)
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    x1 = right - phi * (right - left)
    x2 = left + phi * (right - left)
    s1, s2 = score(x1), score(x2)
    for _ in range(60):
        if s1 <= s2:
            right, x2, s2 = x2, x1, s1
            x1 = right - phi * (right - left)
            s1 = score(x1)
        else:
            left, x1, s1 = x1, x2, s2
            x2 = left + phi * (right - left)
            s2 = score(x2)
    fc = math.exp((left + right) / 2.0)
    rms = math.sqrt(score(math.log(fc)))
    return fc, rms


def measured_minus_3db(points: list[tuple[float, float]]) -> float | None:
    """Find the first -3 dB crossing using log-frequency interpolation."""
    for (f0, d0), (f1, d1) in zip(points, points[1:]):
        if d0 <= -3.0 <= d1 and d1 != d0:
            ratio = (-3.0 - d0) / (d1 - d0)
            return math.exp(math.log(f0) + ratio * math.log(f1 / f0))
    return None


def compare_channel(
    reference: list[tuple[float, float, float]],
    filtered: list[tuple[float, float, float]],
    min_coherence: float,
    fit_max_hz: float,
) -> dict[str, Any]:
    low = max(reference[0][0], filtered[0][0])
    high = min(reference[-1][0], filtered[-1][0])
    # ValveBench normalizes at the FFT bin nearest 1 kHz. With a finite FFT
    # grid, the last bin below '--hi 1000' is often slightly below 1000 Hz.
    # Re-anchor both curves at the nearest common reference bin instead of
    # extrapolating beyond the CSV's measured range.
    common_anchor_bins = [row[0] for row in reference if low <= row[0] <= high]
    if not common_anchor_bins:
        raise ValueError("у bypass и filtered нет общей частотной сетки")
    anchor_hz = min(common_anchor_bins, key=lambda value: abs(value - 1000.0))
    ref_1k = interpolate(reference, anchor_hz)
    filt_1k = interpolate(filtered, anchor_hz)
    if ref_1k is None or filt_1k is None:
        raise ValueError("не удалось интерполировать общий опорный уровень около 1 кГц")

    points: list[tuple[float, float, float]] = []
    for freq, _db, _coh in reference:
        if freq < low or freq > high:
            continue
        ref_value = interpolate(reference, freq)
        filter_value = interpolate(filtered, freq)
        if ref_value is None or filter_value is None:
            continue
        # Each source curve is already normalized around 1 kHz; remove the
        # residual reference-bin/gain offset once more for a clean A/B delta.
        delta = (filter_value[0] - filt_1k[0]) - (ref_value[0] - ref_1k[0])
        coherence = min(ref_value[1], filter_value[1])
        points.append((freq, delta, coherence))

    valid = [(freq, db) for freq, db, coh in points
             if coh >= min_coherence and freq <= fit_max_hz]
    fit = fit_first_order(valid)
    all_valid = [(freq, db) for freq, db, coh in points if coh >= min_coherence]
    return {
        "points": points,
        "valid_points": len(all_valid),
        "fit_points": len(valid),
        "fc_fit_hz": fit[0] if fit else None,
        "fit_rms_db": fit[1] if fit else None,
        "fc_measured_hz": measured_minus_3db(all_valid),
    }


def fmt(value: float | None, precision: int = 2, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.{precision}f}" if signed else f"{value:.{precision}f}"


def render_report(
    reference_path: Path,
    filtered_path: Path,
    channels: dict[str, dict[str, Any]],
    min_coherence: float,
    fit_max_hz: float,
) -> str:
    name = filtered_path.stem
    lines = [
        f"SUBSONIC FILTER A/B · {name}",
        f"Эталон без фильтра: {reference_path}",
        f"Измерение с фильтром: {filtered_path}",
        f"Порог coherence: {min_coherence:.2f}; область подгонки: до {fit_max_hz:g} Гц",
        "Разность АЧХ вычислена как FILTERED − BYPASS; отрицательные значения означают подавление.",
        "",
        "Канал | f-3dB fit | f-3dB измер. | RMS модели | валидных точек",
    ]
    for channel in ("L", "R"):
        result = channels[channel]
        lines.append(
            f"  {channel}   | {fmt(result['fc_fit_hz'], signed=False)} Гц   | "
            f"{fmt(result['fc_measured_hz'], signed=False)} Гц   | "
            f"{fmt(result['fit_rms_db'], signed=False)} дБ   | "
            f"{result['valid_points']}/{len(result['points'])} "
            f"(fit {result['fit_points']})"
        )
    lines += ["", "Частота | подавление L | подавление R | L−R, дБ | модель 1-го порядка"]
    left_points = channels["L"]["points"]
    right_points = channels["R"]["points"]
    for frequency in REPORT_FREQUENCIES_HZ:
        left = interpolate(left_points, frequency)
        right = interpolate(right_points, frequency)
        if left is None or right is None:
            continue
        l_db, l_coh = left[0], left[1]
        r_db, r_coh = right[0], right[1]
        l_txt = fmt(l_db) if l_coh >= min_coherence else f"(coh<{min_coherence:.2f})"
        r_txt = fmt(r_db) if r_coh >= min_coherence else f"(coh<{min_coherence:.2f})"
        spread = l_db - r_db if l_coh >= min_coherence and r_coh >= min_coherence else None
        fits = [channels[c]["fc_fit_hz"] for c in ("L", "R")]
        model = (first_order_db(frequency, sum(fits) / len(fits))
                 if all(value is not None for value in fits) else None)
        lines.append(
            f"{frequency:7.1f} | {l_txt:>12s} | {r_txt:>12s} | "
            f"{fmt(spread):>8s} | {fmt(model):>10s}"
        )
    lines += ["", "Примечания:"]
    for channel in ("L", "R"):
        result = channels[channel]
        if result["fit_rms_db"] is not None and result["fit_rms_db"] > 0.75:
            lines.append(
                f"- {channel}: остаток модели {result['fit_rms_db']:.2f} дБ; "
                "кривая заметно отличается от одиночного RC-фильтра (возможны дополнительные полюса/нагрузка)."
            )
        if result["valid_points"] < max(8, len(result["points"]) // 3):
            lines.append(
                f"- {channel}: мало точек с coherence ≥ {min_coherence:.2f}; "
                "проверь уровень сигнала, подключения и шумовой пол."
            )
    if len(lines) == 0:
        lines.append("Нет данных для отчёта.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сравнить НЧ-АЧХ ValveBench с фильтром и в обход него."
    )
    parser.add_argument("bypass", type=Path, help="CSV измерения в положении BYPASS")
    parser.add_argument("filtered", nargs="+", type=Path,
                        help="один или несколько CSV измерений с фильтром")
    parser.add_argument("--min-coherence", type=float, default=0.80,
                        help="минимальная coherence для анализа (по умолчанию 0.80)")
    parser.add_argument("--fit-max-hz", type=float, default=200.0,
                        help="верхняя частота подгонки однополюсной модели (по умолчанию 200 Гц)")
    parser.add_argument("--json-out", type=Path,
                        help="дополнительно сохранить машинно-читаемый JSON")
    args = parser.parse_args(argv)

    if not 0.0 < args.min_coherence <= 1.0:
        parser.error("--min-coherence должен быть в диапазоне (0, 1]")
    if args.fit_max_hz <= 10.0:
        parser.error("--fit-max-hz должен быть больше 10 Гц")

    try:
        reference = read_valvebench_csv(args.bypass)
        output_results: list[dict[str, Any]] = []
        reports: list[str] = []
        for filtered_path in args.filtered:
            filtered = read_valvebench_csv(filtered_path)
            channels = {
                channel: compare_channel(reference[channel], filtered[channel],
                                         args.min_coherence, args.fit_max_hz)
                for channel in ("L", "R")
            }
            reports.append(render_report(args.bypass, filtered_path, channels,
                                         args.min_coherence, args.fit_max_hz))
            output_results.append({
                "name": filtered_path.stem,
                "reference": str(args.bypass),
                "filtered": str(filtered_path),
                "min_coherence": args.min_coherence,
                "fit_max_hz": args.fit_max_hz,
                "channels": {
                    ch: {key: value for key, value in data.items() if key != "points"}
                    for ch, data in channels.items()
                },
            })
        print("\n\n".join(reports))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps({"analysis": "valvebench-subsonic-filter-ab", "results": output_results},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"\nJSON сохранён: {args.json_out}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"ОШИБКА АНАЛИЗА: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
