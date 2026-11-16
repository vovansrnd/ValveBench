#!/usr/bin/env python3
"""v2.18.5 — программная генерация таблиц Markdown ИЗ итоговых JSON
(ревизия v2.18.5 п.7: таблицы не набираются вручную).

Источники:
  v2185-montecarlo-{white,colored}-summary.json → tables/mc-*.md
  v2182-tone-bounds.json                        → tables/bounds.md
  v2182-resample-exact.json                     → tables/resample.md
  v2185-lsq-pilots.json                         → tables/pilots.md
Запускать из каталога скрипта (пути относительные) или задать --dir.
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def mc_table(model):
    p = Path(__file__).resolve().parent / \
        f"v2185-montecarlo-{model}-summary.json"
    d = json.load(open(p, encoding="utf-8"))
    name = {"white": "белый коррелированный",
            "colored": "цветной (из измеренных PSD)"}[model]
    lines = [f"### MC 500 прогонов/уровень — модель: {name}",
             "",
             "Оценки: prod = центроид dsp.analyze_tone (production), "
             "lsq = независимый LSQ (est_lsq), jGLS = совместный GLS "
             "(est_joint, множитель 2 в σ_pred). err = ppm_est − "
             "PPM_TRUE (−23.5).",
             "",
             "| Уровень | n | prod L bias/σ | prod R bias/σ | prod mean | "
             "lsq L bias/σ | lsq R bias/σ | jGLS bias/σ | σ_pred/факт | "
             "cover 1σ/2σ | статусы L | статусы R |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for rec in d["levels"]:
        e = rec["estimators"]
        sp = rec["sigma_pred_stats"]

        def fmt(k):
            return (f"{e[k]['bias_ppm']:+.2f}/{e[k]['sigma_ppm']:.2f}")
        st_l = ", ".join(f"{s}:{c}" for s, c in
                         sorted(rec["status_counts"]["L"].items())) or "—"
        st_r = ", ".join(f"{s}:{c}" for s, c in
                         sorted(rec["status_counts"]["R"].items())) or "—"
        lines.append(
            f"| {rec['level_dbfs']:+.0f} | {rec['trials']} | {fmt('prod_L')} | "
            f"{fmt('prod_R')} | {fmt('prod_mean')} | {fmt('lsq_L')} | "
            f"{fmt('lsq_R')} | {fmt('joint_gls')} | "
            f"{sp['pred_over_actual']:.2f} | "
            f"{sp['coverage_1sigma_per_trial']:.2f}/"
            f"{sp['coverage_2sigma_per_trial']:.2f} | {st_l} | {st_r} |")
    lines += ["", f"σ_pred (jGLS): средняя предсказанная "
              f"{d['levels'][0]['sigma_pred_stats']['mean_pred_ppm']:.2f} ppm "
              f"на −72 дБFS; фактическая σ и покрытие — в таблице выше.", ""]
    # Топ линий QC по частоте срабатывания — из per-trial CSV (в JSON
    # только уникальные частоты; рассеяние сотен случайных бинов = шум,
    # не линии — ревизия v2.18.5: порог 10 дБ над медианой остатка
    # ложноположителен на белом шуме).
    cnt = Counter()
    base = Path(__file__).resolve().parent
    for p in sorted(base.glob(f"v2185-trials-{model}-batch-*.csv")):
        with open(p) as fh:
            for r in csv.DictReader(fh):
                for ch in ("L", "R"):
                    v = r.get(f"line_hz_{ch}")
                    if v and v != "None":
                        try:
                            cnt[float(v)] += 1
                        except ValueError:
                            pass
    total = sum(cnt.values())
    uniq = len(cnt)
    if total:
        top = ", ".join(f"{h:.1f} Гц ×{c}" for h, c in cnt.most_common(8))
        lines.append(f"QC «линия остатка»: {total} срабатываний, "
                     f"{uniq} уникальных частот — рассеяние по всем бинам "
                     f"означает случайные максимумы шума, а не линии. "
                     f"Топ-8: {top}.")
    else:
        lines.append("QC «линия остатка»: срабатываний нет.")
    lines.append("")
    return "\n".join(lines)


def bounds_table():
    p = Path(__file__).resolve().parent / "v2182-tone-bounds.json"
    d = json.load(open(p, encoding="utf-8"))
    sch = d["schedule"]
    lines = ["### Границы тона в потоке D10 (расписание из констант кода)",
             "",
             f"Расписание: старт {sch['start_s']} с = sleep(0.30) + pad 0.05 "
             f"+ lead 1.5; конец {sch['end_s']} с. Верификация кадрами "
             f"программы: тон в кадре {sch['program_frame_verification']['tone_start_in_program_s']} с — "
             f"{'совпадает' if sch['program_frame_verification']['match'] else 'НЕ совпадает'} "
             f"с lead {sch['program_frame_verification']['expected_lead_s']} с.",
             "",
             "| Захват | план [с] | наблюд. [с] | потеря головы, мс | хвост, мс | "
             "окно production [с] | вылет за конец тона, мс |",
             "|---|---|---|---|---|---|---|"]
    for r in d["results"]:
        lines.append(
            f"| {r['index']} | {r['schedule_start_s']:.2f}–{r['schedule_end_s']:.2f} | "
            f"{r['observed_start_s']:.3f}–{r['observed_end_s']:.3f} | "
            f"{r['head_loss_ms']:.0f} | {r['tail_loss_ms']:+.0f} | "
            f"{r['production_window_s'][0]:.2f}–{r['production_window_s'][1]:.2f} | "
            f"{r['window_overrun_past_tone_end_ms']:+.0f} |")
    lines.append("")
    return "\n".join(lines)


def resample_table():
    p = Path(__file__).resolve().parent / "v2182-resample-exact.json"
    d = json.load(open(p, encoding="utf-8"))
    lines = ["### Квантование resample_poly (1, 4) — ОДИН тон 1 кГц на прогон",
             "",
             "| Длительность, с | padded frames | Δppm точно | остаток min/max, ppm |",
             "|---|---|---|---|"]
    for q in d["quantization"]:
        lines.append(f"| {q['capture_seconds']:.0f} | {q['padded_frames']} | "
                     f"{q['delta_ppm_exact']:.3f} | "
                     f"{q['residual_min_ppm']:+.3f}…{q['residual_max_ppm']:+.3f} |")
    lines += ["", "Численный контроль (после точного компенсирующего "
              "psddpad):", "",
              "| ppm_true | остаток (округлённый), ppm | остаток (дробный), ppm |",
              "|---|---|---|"]
    for r in d["numeric"]:
        lines.append(f"| {r['ppm_true']:+.1f} | {r['residual_ppm_rounded_1k']:+.3f} | "
                     f"{r['residual_ppm_fractional_1k']:+.3f} |")
    lines.append("")
    return "\n".join(lines)


def pilots_table():
    p = Path(__file__).resolve().parent / "v2185-lsq-pilots.json"
    if not p.exists():
        return "### Пилоты — v2185-lsq-pilots.json ещё не создан\n"
    d = json.load(open(p, encoding="utf-8"))
    per_channel = "lsq_status_counts_L" in d["results"][0]
    lines = ["### Пилоты 997/1000/1013 Гц (err относительно ПИЛОТА; "
             "профиль гармоник из WAV"
             + (" с кросс-фазой, оба канала" if per_channel else "") +
             ", детерминированные seed)",
             "",]
    if per_channel:
        lines += ["**Реалистичный профиль** (амплитуды и межканальная фаза "
                  "линий из ансамбля PSD/CSD тихих окон, A = √(2·P)):", "",
                  "| Пилот | prod L bias/σ | prod R bias/σ | LSQ L bias/σ | "
                  "LSQ R bias/σ | статусы QC L | статусы QC R |",
                  "|---|---|---|---|---|---|---|"]
    if per_channel:
        for r in d["results"]:
            if r["mode"] == "measured":
                sc_l = ", ".join(f"{s}:{c}" for s, c in
                                 sorted(r["lsq_status_counts_L"].items())) or "—"
                sc_r = ", ".join(f"{s}:{c}" for s, c in
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
    else:
        # старая одноканальная структура (совместимость)
        lines += ["**Реалистичный профиль** (уровни гармоник измерены из "
                  "ансамбля PSD тихих окон, A = √(2·P), per-channel):", "",
                  "| Пилот | центроид bias/σ | LSQ bias/σ | статусы QC LSQ |",
                  "|---|---|---|---|"]
        for r in d["results"]:
            if r["mode"] == "measured":
                sc = ", ".join(f"{s}:{c}" for s, c in
                               sorted(r["lsq_status_counts"].items())) or "—"
                lines.append(f"| {r['pilot_hz']:.0f} | "
                             f"{r['prod_centroid']['bias_ppm']:+.2f}/"
                             f"{r['prod_centroid']['sigma_ppm']:.2f} | "
                             f"{r['lsq']['bias_ppm']:+.2f}/"
                             f"{r['lsq']['sigma_ppm']:.2f} | {sc} |")
    lines += ["", "**Стресс-тест**: равномерная гребёнка −60 дБFS × 40 "
              "гармоник:", ""]
    if per_channel:
        lines += ["| Пилот | prod L bias/σ | prod R bias/σ | LSQ L bias/σ | "
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
    else:
        lines += ["| Пилот | центроид bias/σ | LSQ bias/σ |", "|---|---|---|"]
        for r in d["results"]:
            if r["mode"] == "uniform_comb":
                lines.append(f"| {r['pilot_hz']:.0f} | "
                             f"{r['prod_centroid']['bias_ppm']:+.2f}/"
                             f"{r['prod_centroid']['sigma_ppm']:.2f} | "
                             f"{r['lsq']['bias_ppm']:+.2f}/"
                             f"{r['lsq']['sigma_ppm']:.2f} |")
    lines.append("")
    return "\n".join(lines)


def table_868():
    p = Path(__file__).resolve().parent / "v2185-868-reproduction.json"
    if not p.exists():
        return "### «868 Гц» — v2185-868-reproduction.json ещё не создан\n"
    d = json.load(open(p, encoding="utf-8"))
    s = d["summary"]
    leg = s["legacy"]
    ex = leg["example_trial"]
    lines = ["### «868 Гц»: воспроизведение механизма и отказ правленого "
             "оценщика",
             "",
             f"Сценарий: {s['scenario']}. Seed: {s['seed_formula']}.", "",
             "| Часть | Показатель | Значение |", "|---|---|---|",
             f"| легаси 8d8b040 | f_est около 868 Гц (±5 Гц) | "
             f"{leg['trials_near_868hz']}/100 |",
             f"| легаси 8d8b040 | невалидная (выпуклая/плоская) "
             f"coarse-парабола | {leg['trials_invalid_coarse_parabola']}/100 |",
             f"| легаси 8d8b040 | смещение параболы > 0.5 бина | "
             f"{leg['trials_coarse_offset_gt_half_bin']}/100 |",
             f"| легаси 8d8b040 | диапазон f_est | {leg['f_est_min_hz']}–"
             f"{leg['f_est_max_hz']} Гц |",
             f"| v2.18.5 правленый | edge_peak (захват отклонён) | "
             f"{s['repaired_v2185']['trials_edge_peak']}/100 |",
             f"| v2.18.5 joint | fail-closed (None, None, fs, статус) | "
             f"{s['joint_fail_closed']['rejected_with_edge_peak']}/100 |",
             "",
             f"Пример (легаси, trial {ex['trial']}): coarse-пик "
             f"{ex['coarse_peak_hz']} Гц (ГРАНИЦА полосы), соседи "
             f"{ex['coarse_neighbors']} (возрастающий склон), знаменатель "
             f"{ex['coarse_denominator']:.3f} — парабола НЕВАЛИДНА, "
             f"смещение {ex['coarse_offset_bins']} бина (неограниченное) → "
             f"f0 {ex['f0_after_coarse_hz']} Гц → f_est {ex['f_est_hz']} Гц.",
             "",
             f"**Вердикт: {s['verdict']}**", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("mc", "bounds", "resample", "pilots"),
                    default=None)
    args = ap.parse_args()
    tab = Path(__file__).resolve().parent / "tables"
    tab.mkdir(exist_ok=True)
    jobs = {
        "mc-white": mc_table("white"),
        "mc-colored": mc_table("colored"),
        "bounds": bounds_table(),
        "resample": resample_table(),
        "pilots": pilots_table(),
        "868": table_868(),
    }
    if args.only:
        jobs = {args.only: jobs[args.only]}
    for name, content in jobs.items():
        (tab / f"{name}.md").write_text(content, encoding="utf-8")
        print(f"tables/{name}.md — {len(content.splitlines())} строк")


if __name__ == "__main__":
    main()
