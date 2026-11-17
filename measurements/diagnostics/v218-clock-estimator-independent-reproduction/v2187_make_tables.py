#!/usr/bin/env python3
"""v2.18.6 — программная генерация таблиц Markdown ИЗ итоговых JSON
(никакого ручного набора; журнал §20.7 п.6).

Источники:
  v2187-montecarlo-{white,colored}-summary.json → tables/mc-*.md
  v2186-qc-confusion.json                       → tables/confusion.md
  v2186-lsq-pilots.json                         → tables/pilots.md
  v2182-tone-bounds.json                        → tables/bounds.md
  v2182-resample-exact.json                     → tables/resample.md
"""
import argparse
import json
from pathlib import Path


def _fmt_stat(s):
    if not s or s.get("n", 0) == 0 or s.get("bias_ppm") is None:
        return "—"
    return f"{s['bias_ppm']:+.2f}/{s['sigma_ppm']:.2f}"


def mc_table(model):
    base = Path(__file__).resolve().parent
    p = base / f"v2187-montecarlo-{model}-summary.json"
    if not p.exists():
        return (f"### MC {model} — v2187-montecarlo-{model}-summary.json "
                f"ещё не создан\n")
    d = json.load(open(p, encoding="utf-8"))
    name = {"white": "белый коррелированный",
            "colored": "цветной line-free (линии сети удалены из модели)"}[model]
    lines = [f"### MC 500 прогонов/уровень — модель: {name}",
             "",
             "Принятые/отвергнутые РАЗДЕЛЬНО (§20.7 п.6). err = ppm_est − "
             "PPM_TRUE (−23.5); формат bias/σ, ppm.",
             "",
             "| Уровень | принято LSQ L/R | lsq L | lsq R | принято jGLS | "
             "jGLS | prod L (без QC) | prod R (без QC) |",
             "|---|---|---|---|---|---|---|---|"]
    for rec in d["levels"]:
        a = rec["accepted"]
        lines.append(
            f"| {rec['level_dbfs']:+.0f} | {a['counts']['lsq_L']}/"
            f"{a['counts']['lsq_R']} | {_fmt_stat(a['lsq_L'])} | "
            f"{_fmt_stat(a['lsq_R'])} | {a['counts']['joint_gls']} | "
            f"{_fmt_stat(a['joint_gls'])} | {_fmt_stat(a['prod_L'])} | "
            f"{_fmt_stat(a['prod_R'])} |")
    lines += ["", "**Отвергнутые** (статусы, какими были бы ошибки без QC):",
              ""]
    for rec in d["levels"]:
        rej = rec["rejected"]
        lines.append(
            f"* {rec['level_dbfs']:+.0f} дБFS: отклонено L "
            f"{rej['counts']['lsq_L']}, R {rej['counts']['lsq_R']}, jGLS "
            f"{rej['counts']['joint_gls']}; статусы L "
            f"{rej['status_counts']['L'] or '—'}; ошибки «как без QC»: "
            f"lsq L {_fmt_stat(rej['raw_errors']['lsq_L'])}, R "
            f"{_fmt_stat(rej['raw_errors']['lsq_R'])}, jGLS "
            f"{_fmt_stat(rej['raw_errors']['joint_gls'])}")
    sp = d["levels"][0]["sigma_pred_stats"]
    if sp["mean_pred_ppm"] is not None:
        lines += ["", f"σ_pred (jGLS, принятые): предсказано "
                  f"{sp['mean_pred_ppm']:.2f} ppm на −72 дБFS, "
                  f"пред/факт {sp['pred_over_actual']}, покрытие "
                  f"1σ/2σ {sp['coverage_1sigma_per_trial']}/"
                  f"{sp['coverage_2sigma_per_trial']}."]
    conf = d["levels"][0]["confusion_negatives"]
    lines += ["", f"Негативы MC (интерференции нет): TN "
              f"{conf['true_negative']}/{conf['true_negative'] + conf['false_positive_line_qc'] + conf['other_refusals']}, "
              f"FP(line-QC) {conf['false_positive_line_qc']}, "
              f"иные отказы {conf['other_refusals']}."]
    lines.append("")
    return "\n".join(lines)


def sweep_table():
    base = Path(__file__).resolve().parent
    d = json.load(open(base / "v2187-qc-sweep.json", encoding="utf-8"))
    s = d["summary"]
    lines = ["### TP/FP/TN/FN калибровки line-QC (v2187_qc_sweep.py)",
             "",
             f"Прогонов: {s['trials_total']} (позитивы {s['positives']}, "
             f"негативы {s['negatives']}, размеры выборки — по ячейкам "
             f"ниже).",
             "",
             "| Класс | Значение |", "|---|---|",
             f"| TP (отказ line_contaminated) | {s['TP']} |",
             f"| — с точной атрибуцией (line_hz ±1.5 Гц) | {s['TP_named_of_TP']} |",
             f"| TP_unnamed (отказ иного типа) | {s['TP_unnamed']} |",
             f"| FN (позитив принят) | {s['FN']} |",
             f"| TN (негатив принят) | {s['TN']} |",
             f"| FP_line (негатив: line_contaminated) | {s['FP_line']} |",
             f"| FP_other (негатив: иной отказ) | {s['FP_other']} |",
             f"| **Полнота (recall)** | **{s['recall']}** |",
             f"| **FPR при пилоте** | **{s['false_positive_rate_with_pilot']}** |",
             f"| **Ложные принятия без пилота** | **{s['FP_false_accept_nopilot']}"
             f"/{s['negatives_no_pilot']}** |",
             "", "Легаси-оценщики на семействе контрпримеров (п.3):",
             ""]
    leg = base / "v2186-legacy-counterexample.json"
    if leg.exists():
        lg = json.load(open(leg, encoding="utf-8"))
        for v in lg["variants"]:
            lines.append(
                f"* 8d8b040, {v['variant']}: медиана "
                f"{v['err_ppm_median']:+.1f} ppm, диапазон "
                f"[{v['err_ppm_min']:+.1f}, {v['err_ppm_max']:+.1f}] "
                f"(n={v['n']}) — принято БЕЗ отказа")
        lines += ["* " + lg["edge_arithmetic"]["explanation"],
                  "* v2.18.6: " + lg["edge_arithmetic"]["v2186_behavior"],
                  ""]
    else:
        lc = d["legacy_counterexample"]
        lines.append(f"* 8d8b040, линия 1000 Гц −30 дБFS: медиана "
                     f"{lc['err_ppm_median']:+.1f} ppm (n={lc['n']}) — "
                     f"принято без отказа; см. также "
                     f"v2186-legacy-counterexample.json")
        lines.append("")
    lines += ["", "### Сетка уровень×смещение (n / отказано / атрибуция / принято FN / |err| принятых):",
              "", "| Ячейка | n | отказано | attr_ok | принято | |err| ppm |",
              "|---|---|---|---|---|---|"]
    for k, v in s.get("grid", {}).items():
        lines.append(f"| {k} | {v['n']} | {v['refused']} | "
                     f"{v['attr_ok']} | {v['accepted']} | "
                     f"{v['err_accepted_ppm'] if v['err_accepted_ppm'] is not None else '—'} |")
    lines.append("")
    return "\n".join(lines)


def pilots_table():
    base = Path(__file__).resolve().parent
    p = base / "v2186-lsq-pilots.json"
    if not p.exists():
        return "### Пилоты — v2186-lsq-pilots.json ещё не создан\n"
    d = json.load(open(p, encoding="utf-8"))
    val = d.get("validation", {})
    lines = ["### Пилоты 997/1000/1013 Гц v2.18.6 (err относительно "
             "ПИЛОТА; профиль гармоник из WAV, калибровка ρ(fh), оба "
             "канала)",
             "",
             f"Калибровка извлечения: ρ(fh) ∈ "
             f"[{d.get('rho_min_max', ['?'])[0]}, "
             f"{d.get('rho_min_max', ['?', '?'])[1] if len(d.get('rho_min_max', [])) > 1 else '?'}] "
             f"(дефект v2.18.5: 0.58–0.65); валидация на внесённых "
             f"линиях: |err| среднее {val.get('err_db_mean_abs')} дБ, "
             f"макс {val.get('err_db_max')} дБ ({val.get('n_lines')} "
             f"линий).",
             "",
             "**Реалистичный профиль** (амплитуды и межканальная фаза "
             "линий из ансамбля PSD/CSD):", "",
             "| Пилот | prod L bias/σ | prod R bias/σ | LSQ L bias/σ | "
             "LSQ R bias/σ | статусы QC L | статусы QC R |",
             "|---|---|---|---|---|---|---|"]
    for r in d["results"]:
        if r["mode"] == "measured":
            sc_l = ", ".join(f"{s_}:{c}" for s_, c in
                             sorted(r["lsq_status_counts_L"].items())) or "—"
            sc_r = ", ".join(f"{s_}:{c}" for s_, c in
                             sorted(r["lsq_status_counts_R"].items())) or "—"
            lines.append(
                f"| {r['pilot_hz']:.0f} | "
                f"{r['prod_centroid_L']['bias_ppm']:+.2f}/"
                f"{r['prod_centroid_L']['sigma_ppm']:.2f} | "
                f"{r['prod_centroid_R']['bias_ppm']:+.2f}/"
                f"{r['prod_centroid_R']['sigma_ppm']:.2f} | "
                f"{r['lsq_L']['bias_ppm']:+.2f}/"
                f"{r['lsq_L']['sigma_ppm']:.2f} | "
                f"{r['lsq_R']['bias_ppm']:+.2f}/"
                f"{r['lsq_R']['sigma_ppm']:.2f} | {sc_l} | {sc_r} |")
    lines += ["", "**Стресс-тест**: равномерная гребёнка −60 дБFS × 40:",
              "",
              "| Пилот | prod L bias/σ | prod R bias/σ | LSQ L bias/σ | "
              "LSQ R bias/σ |", "|---|---|---|---|---|"]
    for r in d["results"]:
        if r["mode"] == "uniform_comb":
            lines.append(
                f"| {r['pilot_hz']:.0f} | "
                f"{r['prod_centroid_L']['bias_ppm']:+.2f}/"
                f"{r['prod_centroid_L']['sigma_ppm']:.2f} | "
                f"{r['prod_centroid_R']['bias_ppm']:+.2f}/"
                f"{r['prod_centroid_R']['sigma_ppm']:.2f} | "
                f"{r['lsq_L']['bias_ppm']:+.2f}/"
                f"{r['lsq_L']['sigma_ppm']:.2f} | "
                f"{r['lsq_R']['bias_ppm']:+.2f}/"
                f"{r['lsq_R']['sigma_ppm']:.2f} |")
    lines.append("")
    return "\n".join(lines)


def bounds_table():
    base = Path(__file__).resolve().parent
    d = json.load(open(base / "v2182-tone-bounds.json", encoding="utf-8"))
    sch = d["schedule"]
    comp = sch.get("components_s", {})
    lines = ["### Границы тона в потоке D10 (расписание из констант кода)",
             "",
             f"Компоненты раздельно (с): sleep backend {comp.get('backend_sleep')}, "
             f"НАЧАЛЬНЫЙ пад backend {comp.get('backend_pre_pad')}, "
             f"КОНЕЧНЫЙ пад backend {comp.get('backend_post_pad')}, "
             f"lead {comp.get('program_lead')}, резерв _seg "
             f"{comp.get('seg_tail_reserve')}.",
             "",
             "| Захват | план [с] | наблюд. [с] | потеря головы, мс | "
             "окно production [с] | вылет, мс |",
             "|---|---|---|---|---|---|"]
    for r in d["results"]:
        lines.append(
            f"| {r['index']} | {r['schedule_start_s']:.2f}–"
            f"{r['schedule_end_s']:.2f} | {r['observed_start_s']:.3f}–"
            f"{r['observed_end_s']:.3f} | {r['head_loss_ms']:.0f} | "
            f"{r['production_window_s'][0]:.2f}–"
            f"{r['production_window_s'][1]:.2f} | "
            f"{r['window_overrun_past_tone_end_ms']:+.0f} |")
    lines.append("")
    return "\n".join(lines)


def resample_table():
    base = Path(__file__).resolve().parent
    d = json.load(open(base / "v2182-resample-exact.json", encoding="utf-8"))
    lines = ["### Квантование resample_poly (1, 4) — ОДИН тон 1 кГц на прогон",
             "",
             "| Длительность, с | padded frames | Δppm точно | остаток min/max, ppm |",
             "|---|---|---|---|"]
    for q in d["quantization"]:
        lines.append(f"| {q['capture_seconds']:.0f} | {q['padded_frames']} | "
                     f"{q['delta_ppm_exact']:.3f} | "
                     f"{q['residual_min_ppm']:+.3f}…"
                     f"{q['residual_max_ppm']:+.3f} |")
    lines += ["", "Численный контроль (после точного компенсирующего "
              "psddpad):", "",
              "| ppm_true | остаток (округлённый), ppm | остаток (дробный), ppm |",
              "|---|---|---|"]
    for r in d["numeric"]:
        lines.append(f"| {r['ppm_true']:+.1f} | "
                     f"{r['residual_ppm_rounded_1k']:+.3f} | "
                     f"{r['residual_ppm_fractional_1k']:+.3f} |")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("mc", "confusion", "pilots",
                                       "bounds", "resample"), default=None)
    args = ap.parse_args()
    tab = Path(__file__).resolve().parent / "tables"
    tab.mkdir(exist_ok=True)
    if args.only == "mc":
        jobs = {"mc-white": mc_table("white"), "mc-colored":
                mc_table("colored")}
    elif args.only:
        fn = {"confusion": confusion_table, "pilots": pilots_table,
              "bounds": bounds_table, "resample": resample_table}[args.only]
        jobs = {args.only: fn()}
    else:
        jobs = {
            "mc-white": mc_table("white"),
            "mc-colored": mc_table("colored"),
            "confusion": sweep_table(),
            "pilots": pilots_table(),
            "bounds": bounds_table(),
            "resample": resample_table(),
        }
    for name, content in jobs.items():
        (tab / f"{name}.md").write_text(content, encoding="utf-8")
        print(f"tables/{name}.md — {len(content.splitlines())} строк")


if __name__ == "__main__":
    main()
