#!/usr/bin/env python3
"""v2.18.2 — генерация Markdown-таблиц НЕПОСРЕДСТВЕННО из JSON/CSV
(ревизия v2.18.1 п.4: численные данные не переписываются вручную).
Читает итоговые JSON v2.18.2 и пишет фрагменты таблиц в tables/.
"""
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLES = HERE / "tables"
TABLES.mkdir(exist_ok=True)


def fmt(x, nd=3):
    return f"{x:.{nd}f}"


def mc_table(model):
    d = json.load(open(HERE / f"v2182-montecarlo-{model}-summary.json"))
    lines = [f"### MC — модель {model.upper()} (500 прогонов × −72/−66/−60)",
             "",
             "| Уровень | n | центроид L σ | центроид R σ | LSQ L σ | LSQ R σ | jGLS σ |",
             "|---|---|---|---|---|---|---|"]
    for lv in d["levels"]:
        e = lv["estimators"]
        lines.append(f"| {lv['level_dbfs']:+.0f} | {lv['trials']} | "
                     f"{e['prod_L']['sigma_ppm']:.3f} | {e['prod_R']['sigma_ppm']:.3f} | "
                     f"{e['lsq_L']['sigma_ppm']:.3f} | {e['lsq_R']['sigma_ppm']:.3f} | "
                     f"{e['joint_gls']['sigma_ppm']:.3f} |")
    lines += ["", "| Уровень | jGLS bias | jGLS max | σ_pred mean | pred/actual | cover 1σ | cover 2σ |",
              "|---|---|---|---|---|---|---|"]
    for lv in d["levels"]:
        e = lv["estimators"]["joint_gls"]
        st = lv["sigma_pred_stats"]
        lines.append(f"| {lv['level_dbfs']:+.0f} | {e['bias_ppm']:+.3f} | "
                     f"{e['max_abs_ppm']:.3f} | {st['mean_pred_ppm']:.3f} | "
                     f"{st['pred_over_actual']:.2f} | "
                     f"{st['coverage_1sigma_per_trial']:.2f} | "
                     f"{st['coverage_2sigma_per_trial']:.2f} |")
    return "\n".join(lines) + "\n"


def pilots_table():
    d = json.load(open(HERE / "v2182-lsq-pilots.json"))
    lines = ["### Пилоты 997/1000/1013 Гц против сетевых гармоник (−72 dBFS)",
             "",
             "err = (f_est/pilot − 1)·1e6 − PPM_TRUE (относительно ПИЛОТА, "
             "исправление ревизии v2.18.1):",
             "",
             "| Пилот | Гармоника | bias ppm | σ ppm | max |ppm| | f_est диапазон, Гц |",
             "|---|---|---|---|---|---|"]
    for r in d["results"]:
        lines.append(f"| {r['pilot_hz']:.0f} | {r['mains_level_dbfs']:+.0f} | "
                     f"{r['bias_ppm']:+.2f} | {r['sigma_ppm']:.2f} | "
                     f"{r['max_abs_ppm']:.2f} | "
                     f"[{r['f_est_min_hz']:.2f}, {r['f_est_max_hz']:.2f}] |")
    inv = d["investigation_868hz"]
    lines += ["", f"**Расследование 868 Гц**: {inv['rerun_result']['conclusion']}. "
              f"Повтор 997/−30: f_est [{inv['rerun_result']['f_est_min_hz']}, "
              f"{inv['rerun_result']['f_est_max_hz']}] Гц (n="
              f"{inv['rerun_result']['trials']}) — committed est_lsq не может "
              f"вернуть 868 Гц (поиск 997±2.15 Гц)."]
    return "\n".join(lines) + "\n"


def bounds_table():
    d = json.load(open(HERE / "v2182-tone-bounds.json"))
    lines = ["### Границы тона: расписание (константы кода) vs измеренные",
             "",
             f"Расписание: {d['schedule']['start_s']}–{d['schedule']['end_s']} с "
             f"({d['schedule']['source'][:80]}…)",
             "",
             "| idx | измерено | голова, мс | хвост, мс | вылет окна, мс |",
             "|---|---|---|---|---|"]
    for r in d["results"]:
        lines.append(f"| {r['index']} | "
                     f"[{r['observed_start_s']:.3f}, {r['observed_end_s']:.3f}] | "
                     f"{r['head_loss_ms']:+.0f} | {r['tail_loss_ms']:+.0f} | "
                     f"{r['window_overrun_past_tone_end_ms']:+.0f} |")
    return "\n".join(lines) + "\n"


def resample_table():
    d = json.load(open(HERE / "v2182-resample-exact.json"))
    lines = ["### Квантование resample (Δppm = 1e6/N_padded)",
             "",
             "| Захват, с | N_padded | Δppm точно | остаток min/max, ppm |",
             "|---|---|---|---|"]
    for r in d["quantization"]:
        lines.append(f"| {r['capture_seconds']:.1f} | {r['padded_frames']} | "
                     f"{r['delta_ppm_exact']:.3f} | "
                     f"[{r['residual_min_ppm']:+.3f}, {r['residual_max_ppm']:+.3f}] |")
    lines += ["", "Численный замер остатка после коррекции (20 кГц):", "",
              "| ppm_true | округлённый | дробный | без коррекции |",
              "|---|---|---|---|"]
    for r in d["numeric"]:
        lines.append(f"| {r['ppm_true']:+.1f} | "
                     f"{r['residual_ppm_rounded_20k']:+.2f} | "
                     f"{r['residual_ppm_fractional_20k']:+.2f} | "
                     f"{r['residual_ppm_none_20k']:+.1f} |")
    return "\n".join(lines) + "\n"


def main():
    (TABLES / "mc-white.md").write_text(mc_table("white"), encoding="utf-8")
    (TABLES / "mc-colored.md").write_text(mc_table("colored"), encoding="utf-8")
    (TABLES / "pilots.md").write_text(pilots_table(), encoding="utf-8")
    (TABLES / "bounds.md").write_text(bounds_table(), encoding="utf-8")
    (TABLES / "resample.md").write_text(resample_table(), encoding="utf-8")
    print("таблицы сгенерированы из JSON/CSV:", TABLES)


if __name__ == "__main__":
    main()
