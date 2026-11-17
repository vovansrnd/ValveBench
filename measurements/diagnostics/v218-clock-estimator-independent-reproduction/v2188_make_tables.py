#!/usr/bin/env python3
"""v2.18.8 — программная генерация таблиц из JSON (§22).

  v2188-snr-compare.json → tables/snr-compare.md
  v2188-nearby-tone.json → tables/nearby-tone.md
"""
import json
from collections import defaultdict
from pathlib import Path


def snr_table():
    base = Path(__file__).resolve().parent
    d = json.load(open(base / "v2188-snr-compare.json", encoding="utf-8"))
    s = d["summary"]
    cls = defaultdict(list)
    for r in d["per_trial"]:
        cls[r["name"].split("#")[0]].append(r)
    lines = ["### Сопоставление SNR-метрик на идентичных записях (§22 п.1)",
             "",
             f"Прогонов: {s['trials']}, конечных пар {s['pairs_finite']}. "
             f"Пороги в матрице решений: prod {s['thresholds']['prod']:.0f} дБ, "
             f"diag {s['thresholds']['diag']:.0f} дБ.",
             "",
             f"Разность diag − prod: медиана "
             f"{s['diff_diag_minus_prod_ppb']['median']:+.1f} дБ "
             f"(std {s['diff_diag_minus_prod_ppb']['std']:.1f}); регрессия "
             f"diag = {s['regression_diag_vs_prod']['slope']:.3f}·prod "
             f"{s['regression_diag_vs_prod']['offset_db']:+.1f} дБ — метрики "
             "НЕ эквивалентны 1:1.",
             "",
             "| Класс | prod mean | diag mean | решений err prod/diag |",
             "|---|---|---|---|"]
    for k, v in sorted(cls.items()):
        prods = [r["prod_snr"] for r in v if r["prod_snr"] is not None]
        diags = [r["diag_snr"] for r in v if r["diag_snr"] is not None]
        pw = sum(1 for r in v if r["prod_decision"] != r["expect"])
        dw = sum(1 for r in v if r["diag_decision"] != r["expect"])
        pm = sum(prods) / len(prods) if prods else float("nan")
        dm = sum(diags) / len(diags) if diags else float("nan")
        lines.append(f"| {k} | {pm:.1f} | {dm:.1f} | {pw}/{len(v)} · "
                     f"{dw}/{len(v)} |")
    lines += ["", "**Матрица решений (SNR-гейты alone):** " +
              ", ".join(f"{k}: {v}" for k, v in
                        sorted(s["decision_matrix_prod_x_diag"].items())) +
              ".", "",
              "**Вердикт:** " + s["verdict"], "",
              "Примечание: clip-класс оба SNR-гейта принимают (гармоники "
              "вне их локальных полос) — клиппинг ловится слоем line-QC "
              "(гармоника 3f₀ −11 дБc → line_contaminated), что и "
              "происходит в полных статусах est_lsq.", ""]
    return "\n".join(lines)


def nearby_table():
    base = Path(__file__).resolve().parent
    d = json.load(open(base / "v2188-nearby-tone.json", encoding="utf-8"))
    s = d["summary"]
    lines = ["### Валидация nearby-tone дискриминатора (§22 п.3, "
             "диагностика)",
             "",
             f"Негативы (биений быть не должно): "
             f"{s['negative_cells']['quiet_correct']}/"
             f"{s['negative_cells']['quiet_expected']} тихо, ложных "
             f"срабатываний {s['negative_cells']['false_fired']}.",
             "",
             "| Соседняя линия | n | обнаружено | hz верно | db верно (±2.5) |",
             "|---|---|---|---|---|"]
    for k, v in sorted(s["per_cell"].items()):
        lines.append(f"| {k} | {v['n']} | {v['detected']} | {v['ok_hz']} | "
                     f"{v['ok_db']} |")
    lines += ["",
              "Граница исключения ±1.5 Гц НЕ менялась; дискриминатор — "
              "информационные поля nearby_tone_hz/nearby_tone_db в est_lsq "
              "(спектральный локальный максимум на |δ| ≥ 0.3 Гц внутри "
              "зоны; структурная утечка якорного фита сосредоточена в "
              "|δ| < 0.02 Гц). Beat-вариант отклонён при отладке: огибающая "
              "«тон + белый шум» не содержит когерентной компоненты на Δ.",
              ""]
    return "\n".join(lines)


def main():
    tab = Path(__file__).resolve().parent / "tables"
    tab.mkdir(exist_ok=True)
    for name, fn in (("snr-compare", snr_table),
                     ("nearby-tone", nearby_table)):
        (tab / f"{name}.md").write_text(fn(), encoding="utf-8")
        print(f"tables/{name}.md готова")


if __name__ == "__main__":
    main()
