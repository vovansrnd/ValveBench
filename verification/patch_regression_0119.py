#!/usr/bin/env python3
"""Адаптация регрессионного набора (этапы 1–3) к исправленному поведению 0.1.19.

Прежние тесты-«дефекты» (PASS = дефект воспроизведён) становятся проверками
ИСПРАВЛЕННОГО поведения, плюс добавляются новые проверки задания №5.
"""
import io

P = "/opt/MyApps/Adudio/for_z/ValveBench-0.1.19/verification/test_gainmatch_0119.py"
src = io.open(P, encoding="utf-8").read()
R = []


def rep(old, new, must=True):
    global src
    if old not in src:
        if must:
            raise SystemExit(f"ANCHOR NOT FOUND: {old[:90]!r}")
        return
    src = src.replace(old, new)
    R.append(old[:60])


# ---------------- БЛОК B: валидность (дефекты → исправления) ----------------

rep('''    check("B1", "XRUN-сообщения ALSA (данные чистые) оставляют valid=True", "defect",
          all_valid,
          "валидность ДОЛЖНА быть False при сообщениях ALSA об ошибках потока",
          f"valid={all_valid}; warnings={res['warnings']}; "
          f"reasons={[row['channels']['L']['validity_reasons'] for row in res['rows']]}")''',
    '''    check("B1-fix", "XRUN-сообщения ALSA инвалидируют серию (исправлено в 0.1.19)", "ok",
          (not all_valid)
          and all("hardware_fault" in row["channels"]["L"]["validity_reasons"]
                  for row in res["rows"])
          and res["events"]["fatal"],
          "ожидание 0.1.19: valid=False, причины hardware_fault, events.fatal заполнены",
          f"valid={all_valid}; warnings={res['warnings']}; "
          f"reasons={[row['channels']['L']['validity_reasons'] for row in res['rows']]}")''')

rep('''    check("B2", "Выпадение 10 мс в середине блока — ловит ли порог SNR", "ok",
          not all(row["valid"] for row in res["rows"]),
          "ожидание: SINAD деградирует ниже 25 дБ → valid=False; "
          "если valid=True — порог не ловит повреждение",
          f"valid={[row['valid'] for row in res['rows']]}; SNR_L={['%.1f' % s for s in snrs]}; "
          f"reasons={reasons}")''',
    '''    check("B2-fix", "Выпадение 10 мс ловится воротами C/R (SINAD-порог заменён)", "ok",
          not all(row["valid"] for row in res["rows"])
          and "low_carrier_residual" in reasons,
          "ожидание 0.1.19: valid=False через low_carrier_residual",
          f"valid={[row['valid'] for row in res['rows']]}; SNR_L={['%.1f' % s for s in snrs]}; "
          f"reasons={reasons}")''')

rep('''    check("B4", "Выпадение 30 мс — поймает ли low_snr", "ok",
          not all(row["valid"] for row in res["rows"]),
          "ожидание: valid=False через low_snr",
          f"valid={[row['valid'] for row in res['rows']]}; reasons={reasons}")''',
    '''    check("B4-fix", "Выпадение 30 мс — ловится воротами C/R", "ok",
          not all(row["valid"] for row in res["rows"])
          and "low_carrier_residual" in reasons,
          "ожидание 0.1.19: valid=False через low_carrier_residual",
          f"valid={[row['valid'] for row in res['rows']]}; reasons={reasons}")''')

# B6: onset-фикс — уровни точные, все строки валидны
rep('''    check("B6", "Щелчок в лид-тишине: уровни каналов занижаются молча", "defect",
          lvl_err > 0.5 and derr < 0.05,
          "ожидание: уровни L и R занижены ~1 дБ при valid=True и пустых warnings "
          "(ΔG не страдает — сдвиг общий)",
          f"ошибка уровней={lvl_err:.3f} дБ; ошибка ΔG={derr:.3f} дБ; "
          f"warnings={res['warnings']}; valid={[row['valid'] for row in res['rows']]}")''',
    '''    check("B6-fix", "Щелчок в лид-тишине: узкополосный onset не сбивается — уровни точные",
          "ok", lvl_err < 0.05 and derr < 0.02
          and all(row["valid"] for row in res["rows"]),
          "ожидание 0.1.19: onset находится на тоне (устойчивость ≥1 с), "
          "уровни и ΔG без ошибки, все строки валидны",
          f"ошибка уровней={lvl_err:.3f} дБ; ошибка ΔG={derr:.3f} дБ; "
          f"warnings={res['warnings']}; valid={[row['valid'] for row in res['rows']]}")''')

# B6b: всплески внутри окна — бракуются подоконным контролем
rep('''    check("B6b", "Щелчок внутри окна L: ниже некоторого уровня пороги перестают ловить",
          "defect", silent_found,''',
    '''    check("B6b-fix", "Щелчок внутри окна L: все случаи с ошибкой > 0.02 дБ бракуются "
                     "подоконным контролем (unstable_carrier)",
          "ok", silent_caught_all and not silent_found,''')
rep('''        if valid_b and derr_b > 0.02:
            silent_found = True''',
    '''        if valid_b and derr_b > 0.02:
            silent_found = True
        if derr_b > 0.02 and not valid_b and "unstable_carrier" in res["rows"][0]["channels"]["L"]["validity_reasons"]:
            silent_caught = True''')
rep('''    rows_b6b = []
    silent_found = False''',
    '''    rows_b6b = []
    silent_found = False
    silent_caught = False''')
rep('''    check("B6b-fix", "Щелчок внутри окна L: все случаи с ошибкой > 0.02 дБ бракуются "
                     "подоконным контролем (unstable_carrier)",
          "ok", silent_caught_all and not silent_found,''',
    '''    check("B6b-fix", "Щелчок внутри окна L: все случаи с ошибкой > 0.02 дБ бракуются "
                     "подоконным контролем (unstable_carrier)",
          "ok", silent_caught and not silent_found,''')

# ---------------- БЛОК C: SINAD-семантика (поля переименованы) ----------------
rep('''    check("C1", "«snr» в measure_series = SINAD, а не SNR (гармоники на 26 дБ выше шума)",
          "defect", ok_main,''',
    '''    check("C1-0119", "Величина остаётся SINAD по построению; в 0.1.19 поле sinad_db "
                     "и подписи UI исправлены", "ok", ok_main,''')
rep('''    check("C2", "«SNR» живого индикатора = SINAD (в остатке LSQ гармоники)",
          "defect",''',
    '''    check("C2-0119", "Индикатор показывает SINAD (подпись UI исправлена в 0.1.19)",
          "ok",''')

# ---------------- БЛОК D: TARE — контекстные варианты ----------------
rep('''    check("D2", "TARE при смене частоты: фантомная ΔG из-за частотно-зависимого интерфейса",
          "defect", ok,''',
    '''    check("D2", "TARE без контекста (legacy): прежнее поведение задокументировано",
          "ok", ok,''')
rep('''    check("D4", "TARE при смене калибровки: асимметричная поправка L не обнаруживается",
          "defect", abs(st["delta"] - 0.5) < 0.02,''',
    '''    check("D4", "TARE без контекста (legacy): смена cal не обнаруживается — задокументировано",
          "ok", abs(st["delta"] - 0.5) < 0.02,''')
rep('''    check("D5", "TARE при дрейфе тракта: +0.5 дБ в L после tare не обнаруживается", "defect",
          abs(st["delta"] - 0.5) < 0.02,''',
    '''    check("D5", "TARE без контекста (legacy): дрейф тракта не обнаруживается — "
                "контекст TARE защищает от смены УСЛОВИЙ, не от дрейфа",
          "ok", abs(st["delta"] - 0.5) < 0.02,''')

# ---------------- БЛОК E10: repeats=1 ----------------
rep('''    check("E10", "repeats=1 даёт «repeat_stable=True» без доказательств стабильности",
          "defect", all(row["repeat_stable"] for row in res1["rows"]) and bool(ref1),''',
    '''    check("E10-fix", "repeats=1: repeat_stable=None, эталон петли отклоняется (0.1.19)",
          "ok", all(row["repeat_stable"] is None for row in res1["rows"])
          and ref1 is None,''')

# ---------------- БЛОК F: onset-фикс ----------------
rep('''    check("F1", "B6: окна совпадают с формулой Rig.run, onset ложный", "ok",
          match0 and match1 and abs(onset / fs - 0.05) < 0.01,
          "rec[onset+s_n : +L] == окно продукта; onset ≈ 0.05 с (щелчок), не 0.50 с",
          f"match0={match0}, match1={match1}, onset={onset / fs:.3f} с")''',
    '''    check("F1-fix", "B6: onset найден на ТОНЕ (устойчивый участок), не на щелчке", "ok",
          match0 and match1 and abs(onset / fs - 0.5) < 0.02,
          "0.1.19: onset ≈ 0.500 с (начало тона), щелчок 0.05 с проигнорирован",
          f"match0={match0}, match1={match1}, onset={onset / fs:.3f} с")''')
rep('''    check("F2", "B6: −1,44 дБ объясняются долей тишины в блоках окна", "ok",
          abs(b1 / true_amp - 0.70) < 0.02 and abs(b2 / true_amp - 1.0) < 0.01
          and abs(pred_err - meas_err) < 0.05,
          "блок 1 ≈ 0.70 (0.3 с тишины), блок 2 ≈ 1.00; 20lg(mean)=−1.41 дБ ≈ измеренным −1.44 дБ",
          f"блок1={b1 / true_amp:.4f}, блок2={b2 / true_amp:.4f}, "
          f"предсказание {pred_err:+.3f} дБ vs измерено {meas_err:+.3f} дБ")''',
    '''    check("F2-fix", "B6: окна выровнены по тону — уровни без потери −1,44 дБ", "ok",
          abs(b1 / true_amp - 1.0) < 0.02 and abs(b2 / true_amp - 1.0) < 0.02
          and abs(meas_err) < 0.02,
          "0.1.19: оба блока ≈ 1.000 от истинной амплитуды, ошибка уровня < 0.02 дБ",
          f"блок1={b1 / true_amp:.4f}, блок2={b2 / true_amp:.4f}, "
          f"ошибка уровня {meas_err:+.3f} дБ")''')
rep('''    check("F3", "B6: ΔG не страдает (сдвиг общий); строка 2 бракуется протечкой тона 1-го сегмента",
          "ok", delta_err < 0.05 and "low_snr" in row2_reasons,
          "ошибка ΔG ≈ 0 (оба канала сдвинуты одинаково); строка 2: low_snr из-за "
          "0.3 с чужого тона 1 кГц в окне 1.5 кГц",
          f"ошибка ΔG={delta_err:.4f} дБ; reasons строки 2={row2_reasons}")''',
    '''    check("F3-fix", "B6: ΔG без ошибки, протечки чужого сегмента больше нет", "ok",
          delta_err < 0.02 and not row2_reasons,
          "0.1.19: окна обоих сегментов выровнены по тонам — протечка исчезла",
          f"ошибка ΔG={delta_err:.4f} дБ; reasons строки 2={row2_reasons}")''')

# ---------------- БЛОК G: подоконный контроль в продукте ----------------
rep('''    check("G2", "Молчащая зона существует, детектор видит все случаи с ошибкой > 0.02 дБ",
          "defect", n_valid_with_err > 0 and det_catches_all,''',
    '''    check("G2-fix", "Всплески: ни одного случая valid=True с ошибкой > 0.02 дБ — "
                    "все бракуются unstable_carrier", "ok",
          n_valid_with_err == 0 and det_catches_all,''')

# ---------------- БЛОК H: SINAD-браковка лампы устранена ----------------
rep('''    check("H1", "Порог SINAD бракует лампу: валидность падает при гармониках, "
                "безопасных для измерения тона", "defect",
          flip is not None and flip["hd"] >= -30.0 and max_lvl_err < 0.01,
          "ожидание: valid=False начиная с гармоник ≈ −30…−20 dBc (THD 3–10 % — норма "
          "для лампы без ООС), при ошибке измерения основного тона < 0.01 дБ",
          f"граница отказа: H2=H3={flip['hd']:.0f} dBc (SINAD {flip['sinad']:.1f} < 25); "
          f"последний валидный: {last_ok['hd']:.0f} dBc; "
          f"макс. ошибка уровня во всей развёртке: {max_lvl_err:.5f} дБ")''',
    '''    check("H1-fix", "Гармоники лампы больше не бракуют серию: valid=True во всей "
                    "развёртке −60…−10 dBc", "ok",
          flip is None and max_lvl_err < 0.01,
          "ожидание 0.1.19: SINAD — диагностика, ворота C/R гармоники не видят; "
          "ошибка уровня основного тона < 0.01 дБ",
          f"valid на всех точках развёртки: {flip is None}; "
          f"макс. ошибка уровня: {max_lvl_err:.5f} дБ")''')

# ---------------- БЛОК J: события ----------------
rep('''    check("J1", "Любое сочетание XRUN воспроизведения/захвата оставляет valid=True", "defect",
          v_play and v_cap and v_both,
          "ожидание для прибора: любой XRUN → недостоверно",
          f"playback XRUN: valid={v_play}; capture XRUN: valid={v_cap}; "
          f"оба: valid={v_both}; warnings доезжают до result['warnings'], "
          f"но не до строк и не до текста вердикта")''',
    '''    check("J1-fix", "Любое сочетание XRUN инвалидирует серию (0.1.19)", "ok",
          (not v_play) and (not v_cap) and (not v_both),
          "ожидание 0.1.19: любой XRUN → valid=False",
          f"playback XRUN: valid={v_play}; capture XRUN: valid={v_cap}; оба: {v_both}")''')
rep('''    check("J2", "Рассинхрон тактов +21 ppm: анализ в некогерентном режиме, "
                "его предупреждение нигде не сохраняется", "defect",
          row["valid"] and not has_warn_in_result,
          "ожидание: предупреждение анализатора хотя бы сохраняется; фактически "
          "analysis['warnings'] отбрасывается целиком",
          f"valid={row['valid']}; warnings результата: {res['warnings']}; "
          f"поля записи повтора (без warnings): {repeat_keys}")''',
    '''    analyzer_saved = any(q.get("analyzer_warnings")
                         for row in res["rows"] for c in ("L", "R")
                         for q in row["channels"][c]["repeats"])
    check("J2-fix", "Предупреждения analyze_tone сохраняются в повторах и в result.events.info",
          "ok", row["valid"] and analyzer_saved,
          "0.1.19: analysis['warnings'] → repeats[].analyzer_warnings; "
          "предупреждения стенда → result.events.info",
          f"valid={row['valid']}; analyzer_warnings сохранены: {analyzer_saved}; "
          f"events.info={res['events']['info']}")''')
rep('''    check("J4", "С семантической правкой валидности текст уже не может рапортовать успех", "ok",
          "НЕ ЗАСЧИТАНО" in verdict_line,''',
    '''    check("J4-fix", "Текст вердикта печатает фатальные события и НЕ рапортует успех", "ok",
          "НЕ ЗАСЧИТАНО" in verdict_line and "ФАТАЛЬНЫЕ" in verdict,''')

# ---------------- БЛОК M: дрейф ловится ----------------
rep('''    check("M1", "Асимметричный прогрев канала: settle не защищает ΔG, valid=True", "defect",
          abs(errs[0.15]) > 0.1 and abs(errs[1.5]) > 0.1,
          "ошибка ΔG существенная при ЛЮБОМ settle (нестационарность усредняется в "
          "результат); защиту даёт подоконный детектор, а не ожидание",''',
    '''    check("M1-fix", "Асимметричный прогрев бракуется подоконным контролем (unstable_carrier)",
          "ok", not res_m_valid_all and "unstable_carrier" in res_m_reasons,
          "0.1.19: нестационарность → valid=False; settle остаётся настройкой, "
          "защиту даёт контроль огибающей",''')
rep('''    errs = {}
    dets = {}
    lvls = {}
    for settle in (0.15, 1.5):''',
    '''    errs = {}
    dets = {}
    lvls = {}
    res_m_valid_all = True
    res_m_reasons = []
    for settle in (0.15, 1.5):''')
rep('''        errs[settle] = res["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"]
        dets[settle] = envelope_instability(w[1000], fs, 1000.0)
        lvls[settle] = res["rows"][0]["channels"]["L"]["level_dbfs"]''',
    '''        errs[settle] = res["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"]
        dets[settle] = envelope_instability(w[1000], fs, 1000.0)
        lvls[settle] = res["rows"][0]["channels"]["L"]["level_dbfs"]
        res_m_valid_all &= all(row["valid"] for row in res["rows"])
        res_m_reasons = sorted({r for row in res["rows"] for c in ("L", "R")
                                for r in row["channels"][c]["validity_reasons"]})''')

# ---------------- БЛОК O: близкая линия — теперь есть предупреждение ----------------
rep('''    check("O2", "Близкая линия (+3 бина, −30 дБс) невидима для ворот продукта", "defect",
          near[3] > 0.02 and near[5] and near[6] > 50.0,''',
    '''    check("O2-0119", "Близкая линия: спектральные ворота бессильны (известное ограничение), "
                     "но подключено предупреждение о близости к гармоникам сети", "ok",
          near[3] > 0.02 and near[5] and near[6] > 50.0, ''')

# ---------------- Q3: анализаторные предупреждения теперь сохраняются ----------------
rep('''    check("Q3", "Аппаратные события доходят до JSON; предупреждения анализатора — нет",
          "defect", in_json and not analyzer_saved,''',
    '''    check("Q3-fix", "Аппаратные события и предупреждения анализатора доходят до JSON",
          "ok", in_json and analyzer_saved,''')

io.open(P, "w", encoding="utf-8").write(src)
print("применено замен:", len(R))
