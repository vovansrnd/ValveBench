#!/usr/bin/env python3
"""Регрессионные тесты Rig.run() и edge-защиты (v2.11, задание ревизии v2.10).

Скриптовый эмулятор hardware (ScriptedHW) рендерит программу с управляемыми
дефектами реального тракта: поканальный gain, щелчок открытия, дрейф такта,
остаток предыдущего потока, потеря хвоста, шум, dropout канала.

Покрываются требования ревизии:
  * несколько последовательных сегментов; разные settle у сегментов;
  * lead/tail добавляются ровно ОДИН раз вокруг всей программы;
  * clock drift;
  * щелчок до тона (регрессия B6 — onset не должен сработать);
  * шум и тихий DUT (относительная проверка устойчивости без абсолютных
    порогов);
  * Gain Match с независимыми L/R;
  * dropout одного канала на 100 мс при нормальном другом → отказ
    с именем канала, в ОБЕ стороны (L пропадает / R пропадает).

Запуск: python3 verification/test_rig_run_regression.py  (или pytest).
"""
import sys
import traceback
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from valvebench import dsp, measure as m, calib  # noqa: E402

FS = 48000
TONE_DBFS = -30.0


class ScriptedHW:
    """Эмулятор hardware: play_record(program) → захват с дефектами.

    pre_s: длительность «до программы» в начале захвата (потеря края/сдвиг
        рендера — программа появляется в захвате на pre_s позже);
    pre_junk: (f_hz, level_dbfs) — остаток ПРЕДЫДУЩЕГО потока, заполняющий
        pre-область (None → цифровая тишина);
    tail_cut_s: обрезанные кадры в конце захвата (потеря хвоста);
    ppm: дрейф такта рендера относительно захвата (ресемплинг);
    click: (t_s, level_dbfs, dur_s) — щелчок открытия ЦАП;
    noise_dbfs: белый шум по обоим каналам;
    dropout: (ch, rel_to_tone_s, dur_s) — цифровой ноль канала ch,
        отсчёт от НАЧАЛА ТОНА в программе (обнаруживается по факту);
    dead_channel: 0/1 — канал полностью цифровой ноль на ВСЁМ захвате
        (постоянное отключение канала);
    dead_channel_noise: (ch, level_dbfs) — канал заменён аналоговым полем
        (белый шум уровня level) на всём захвате — «отцеплённый кабель»:
        не цифровой ноль, тон отсутствует;
    gain: (gL, gR) — поканальный gain DUT.
    """

    def __init__(self, pre_s=0.0, pre_junk=None, tail_cut_s=0.0, ppm=0.0,
                 click=None, noise_dbfs=None, dropout=None, gain=(1.0, 1.0),
                 dead_channel=None, dead_channel_noise=None):
        self.fs = FS
        self.pre_s = pre_s
        self.pre_junk = pre_junk
        self.tail_cut_s = tail_cut_s
        self.ppm = ppm
        self.click = click
        self.noise_dbfs = noise_dbfs
        self.dropout = dropout
        self.gain = gain
        self.dead_channel = dead_channel
        self.dead_channel_noise = dead_channel_noise
        self.notes = []
        self.programs = []

    def open(self, fs):
        self.fs = int(fs)

    def play_record(self, program):
        prog = np.asarray(program, dtype=np.float64)
        self.programs.append(prog.copy())
        n = len(prog)
        # поканальный gain DUT
        rec = prog * np.array(self.gain, dtype=np.float64)[None, :]
        # щелчок открытия ЦАП (короткий всплеск в тишине до тона)
        if self.click is not None:
            t_s, lvl, dur = self.click
            mm = int(dur * self.fs)
            i0 = int(t_s * self.fs)
            tt = np.arange(mm) / self.fs
            burst = dsp.dbfs_to_amp(lvl) * np.sin(2 * np.pi * 1000.0 * tt)
            rec[i0:i0 + mm, :] += burst[:, None]
        # дрейф такта: рендер идёт с частотой (1+ppm) относительно захвата
        if self.ppm:
            ratio = 1.0 + self.ppm * 1e-6
            mm = int(round(n * ratio))
            pos = np.arange(mm) / ratio
            drifted = np.empty((mm, 2))
            for c in (0, 1):
                drifted[:, c] = np.interp(pos, np.arange(n), rec[:, c])
            rec = drifted
        # pre-область: остаток предыдущего потока или тишина (программа
        # появляется в захвате позже на pre_s — потеря края потока)
        pre_n = int(round(self.pre_s * self.fs))
        if pre_n > 0:
            if self.pre_junk is not None:
                f_hz, lvl = self.pre_junk
                tt = np.arange(pre_n) / self.fs
                pre = np.tile(dsp.dbfs_to_amp(lvl) *
                              np.sin(2 * np.pi * f_hz * tt)[:, None], (1, 2))
            else:
                pre = np.zeros((pre_n, 2))
            rec = np.concatenate([pre, rec])
        # потеря хвоста потока
        if self.tail_cut_s > 0:
            rec = rec[:len(rec) - int(round(self.tail_cut_s * self.fs))]
        # белый шум
        if self.noise_dbfs is not None:
            rng = np.random.default_rng(20261)
            rec = rec + dsp.dbfs_to_amp(self.noise_dbfs) * \
                rng.standard_normal(rec.shape)
        # dropout канала: от начала ТОНА в программе (обнаруживается по факту)
        if self.dropout is not None:
            ch, rel_s, dur_s = self.dropout
            nz = np.max(np.abs(prog), axis=1)
            tone0 = int(np.argmax(nz > 1e-5))
            i0 = pre_n + tone0 + int(round(rel_s * self.fs))
            mm = int(dur_s * self.fs)
            rec[i0:i0 + mm, ch] = 0.0
        # постоянное отключение канала: цифровой ноль или аналоговое поле
        if self.dead_channel is not None:
            rec[:, self.dead_channel] = 0.0
        if self.dead_channel_noise is not None:
            ch, lvl = self.dead_channel_noise
            rng = np.random.default_rng(20262)
            rec[:, ch] = dsp.dbfs_to_amp(lvl) * rng.standard_normal(len(rec))
        return rec


def make_rig(hw, edge_protect=True, blocks=2, settle=0.5):
    rig = m.Rig(hw, calib.Calibration(), fs=FS, df=1.0, blocks=blocks,
                settle=settle, edge_protect=edge_protect)
    rig.open()
    return rig


def demod_amp_dbfs(x, fs, f, span_s=1.0):
    """Амплитуда тона f в средней части сигнала x, dBFS (устойчива к drift)."""
    n = len(x)
    w = min(n, int(span_s * fs))
    seg = x[(n - w) // 2:(n - w) // 2 + w]
    t = np.arange(len(seg)) / fs
    z = seg * np.exp(-2j * np.pi * f * t)
    return 20 * np.log10(max(2.0 * float(np.abs(np.mean(z))), 1e-12))


def tone_seg(rig, f, dbfs):
    k, a = rig.amp_tone(f, dbfs)
    return (k, a), k


# ---------------------------------------------------------------- тесты

def test_multi_segment_and_settles():
    """Два последовательных сегмента с РАЗНЫМИ settle: два окна blocks·n,
    каждое содержит СВОЙ тон (частотная разделимость)."""
    hw = ScriptedHW()
    rig = make_rig(hw, edge_protect=True)
    (kA, aA), _ = tone_seg(rig, 1000.0, -20.0)
    (kB, aB), _ = tone_seg(rig, 500.0, -25.0)
    wins, warns = rig.run([([(kA, aA)], [(kA, aA)], None),
                           ([(kB, aB)], [(kB, aB)], 0.3)])
    assert len(wins) == 2, f"ожидались 2 окна, получено {len(wins)}"
    n_win = rig.blocks * rig.n
    for i, w in enumerate(wins):
        assert len(w) == n_win, f"окно {i}: {len(w)} != blocks·n ({n_win})"
    a1_1000 = demod_amp_dbfs(wins[0][:, 0], FS, 1000.0)
    a1_500 = demod_amp_dbfs(wins[0][:, 0], FS, 500.0)
    a2_500 = demod_amp_dbfs(wins[1][:, 0], FS, 500.0)
    a2_1000 = demod_amp_dbfs(wins[1][:, 0], FS, 1000.0)
    assert abs(a1_1000 - (-20.0)) < 0.5, f"окно 1 @1000 Гц: {a1_1000:.1f}"
    assert a1_500 < -50, f"окно 1 утечка @500 Гц: {a1_500:.1f}"
    assert abs(a2_500 - (-25.0)) < 0.5, f"окно 2 @500 Гц: {a2_500:.1f}"
    assert a2_1000 < -55, f"окно 2 утечка @1000 Гц: {a2_1000:.1f}"


def test_lead_tail_added_exactly_once():
    """Lead и tail добавляются ровно ОДИН раз вокруг всей программы (не
    вокруг каждого сегмента): суммарная длина, крайние зоны — нули."""
    hw = ScriptedHW()
    rig = make_rig(hw, edge_protect=True)
    (kA, aA), _ = tone_seg(rig, 1000.0, -20.0)
    (kB, aB), _ = tone_seg(rig, 500.0, -25.0)
    rig.run([([(kA, aA)], [(kA, aA)], None),
             ([(kB, aB)], [(kB, aB)], 0.3)])
    assert len(hw.programs) == 1
    prog = hw.programs[0]
    lead_n = int(round(rig.edge_protect_lead_s * FS))
    tail_n = int(round(rig.edge_protect_tail_s * FS))
    # два сегмента: если бы lead/tail добавлялись на сегмент, длина была бы
    # больше на (2-1)×(lead+tail)
    seg_total = len(prog) - lead_n - tail_n
    expect_seg = (int(round((rig.settle + rig.blocks * 1.0 + 0.05) * FS)) +
                  int(round((0.3 + rig.blocks * 1.0 + 0.05) * FS)))
    assert seg_total == expect_seg, \
        f"сегменты: {seg_total} != ожидаемые {expect_seg} (lead/tail не один раз?)"
    assert np.all(prog[:lead_n] == 0.0), "lead-зона не тишина"
    assert np.all(prog[-tail_n:] == 0.0), "tail-зона не тишина"
    assert np.max(np.abs(prog[lead_n:len(prog) - tail_n])) > 0.05, \
        "в середине программы нет сигнала"
    # при выключенной защите длина = сегменты ровно (pre_s — pre-roll
    # arecord, как в production: onset требует pre-tone тишину)
    hw2 = ScriptedHW(pre_s=0.5)
    rig2 = make_rig(hw2, edge_protect=False)
    rig2.run([([(kA, aA)], [(kA, aA)], None)])
    assert len(hw2.programs[0]) == int(round((rig2.settle + 2.0 + 0.05) * FS)), \
        "без edge-защиты программа должна быть без lead/tail"


def test_clock_drift():
    """Дрейф такта 50 ppm (за пределом допуска проверки такта): run()
    завершается, окно содержит тон с корректной амплитудой."""
    hw = ScriptedHW(ppm=50.0)
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 1], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.6, f"дрейф 50 ppm: амплитуда {got:.1f}"


def test_click_before_tone():
    """Щелчок открытия ЦАП (−20 dBFS, 5 мс) в тишине ДО тона: onset не
    срабатывает на щелчке (регрессия B6), окно попадает на тон."""
    hw = ScriptedHW(click=(0.3, -20.0, 0.005))
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 0], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.5, \
        f"onset сработал на щелчке? амплитуда окна {got:.1f}"


def test_junk_burst_before_lead():
    """Остаток ПРЕДЫДУЩЕГО потока (1000 Гц −55 dBFS, 0.25 с) в начале
    захвата до lead-тишины: короткая вспышка отклоняется sustain-критерием,
    окно попадает на тон (сценарий D10-класса с quiet-остатком)."""
    hw = ScriptedHW(pre_s=0.35, pre_junk=(1000.0, -55.0))
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 0], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.5, f"окно после junk-вспышки: {got:.1f}"


def test_noise_and_quiet_dut():
    """Тихий DUT (−60 dBFS) на шумовом полу (−85 dBFS): относительная
    проверка устойчивости не отбрасывает тихие DUT, амплитуда корректна."""
    hw = ScriptedHW(noise_dbfs=-85.0)
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, -60.0)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 1], FS, 1000.0)
    assert abs(got - (-60.0)) < 0.8, f"тихий DUT на шуме: {got:.1f}"


def test_gain_match_independent_lr():
    """Независимые уровни L/R в одном сегменте (Gain Match): окна отражают
    оба уровня; поканальная проверка устойчивости не принимает более громкий
    канал за замену более тихого."""
    hw = ScriptedHW()
    rig = make_rig(hw, edge_protect=True)
    (k, _), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    aL = dsp.dbfs_to_amp(-30.0)
    aR = dsp.dbfs_to_amp(-40.0)
    wins, warns = rig.run([([(k, aL)], [(k, aR)], None)])
    got_l = demod_amp_dbfs(wins[0][:, 0], FS, 1000.0)
    got_r = demod_amp_dbfs(wins[0][:, 1], FS, 1000.0)
    assert abs(got_l - (-30.0)) < 0.5, f"L: {got_l:.1f}"
    assert abs(got_r - (-40.0)) < 0.5, f"R: {got_r:.1f}"
    # DUT с независимыми gain L/R: подано одинаково, захвачено по-разному
    hw2 = ScriptedHW(gain=(1.0, 0.5))
    rig2 = make_rig(hw2, edge_protect=True)
    (k2, a2), _ = tone_seg(rig2, 1000.0, TONE_DBFS)
    wins2, _ = rig2.run([([(k2, a2)], [(k2, a2)], None)])
    got2_l = demod_amp_dbfs(wins2[0][:, 0], FS, 1000.0)
    got2_r = demod_amp_dbfs(wins2[0][:, 1], FS, 1000.0)
    assert abs(got2_l - (-30.0)) < 0.5, f"DUT gain L: {got2_l:.1f}"
    assert abs(got2_r - (-36.0)) < 0.5, f"DUT gain R (0.5×): {got2_r:.1f}"


def test_dropout_channel_l_fails():
    """L пропадает на 100 мс при нормальном R → отказ с именем канала L."""
    hw = ScriptedHW(dropout=(0, 1.0, 0.1))
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "канал L" in str(exc), f"причина не называет канал L: {exc}"
        return
    raise AssertionError("dropout L не привёл к отказу (замаскирован R?)")


def test_dropout_channel_r_fails():
    """R пропадает на 100 мс при нормальном L → отказ с именем канала R."""
    hw = ScriptedHW(dropout=(1, 1.0, 0.1))
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "канал R" in str(exc), f"причина не называет канал R: {exc}"
        return
    raise AssertionError("dropout R не привёл к отказу (замаскирован L?)")


def test_no_dropout_passes():
    """Контрольная: без dropout тот же сценарий проходит (отказ именно от
    провала, не от конфигурации)."""
    hw = ScriptedHW()
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 0], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.5


# ------------------------------------------------- v2.12 (ревизия v2.11)

def test_dead_channel_l_detected():
    """Постоянно отключённый L (цифровой ноль) при тоне в ОБА канала →
    отказ с именем L (собственная медиана молчащего канала тоже нулевая —
    временная проверка (а) не сработала бы; ловит структурная (б))."""
    hw = ScriptedHW(dead_channel=0)
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "канал L" in str(exc), f"причина не называет канал L: {exc}"
        return
    raise AssertionError("молчащий L не обнаружен (rig.run прошёл молча)")


def test_dead_channel_r_detected():
    """Постоянно отключённый R (цифровой ноль) при тоне в ОБА канала →
    отказ с именем R."""
    hw = ScriptedHW(dead_channel=1)
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "канал R" in str(exc), f"причина не называет канал R: {exc}"
        return
    raise AssertionError("молчащий R не обнаружен (rig.run прошёл молча)")


def test_dead_channel_analog_l_detected():
    """«Отцеплённый кабель»: L заменён аналоговым полем (−80 дБФС, НЕ
    цифровой ноль) при тоне −30 в оба канала → межканальная проверка (в)
    обнаруживает отсутствие сигнала в L относительно R."""
    hw = ScriptedHW(dead_channel_noise=(0, -80.0))
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "канал L" in str(exc), f"причина не называет канал L: {exc}"
        return
    raise AssertionError("аналогово-мёртвый L не обнаружен")


def test_route_l_only_passes():
    """Маршрут L-only: тон подан только в L, R намеренно молчит —
    незадействованный канал НЕ бракуется, измерение проходит."""
    hw = ScriptedHW()
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [], None)])
    got = demod_amp_dbfs(wins[0][:, 0], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.5


def test_route_r_only_passes():
    """Маршрут R-only: тон подан только в R, L намеренно молчит."""
    hw = ScriptedHW()
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 1], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.5


def test_gain_imbalance_within_tolerance_passes():
    """Легитимный дисбаланс DUT (R на 6 дБ тише) НЕ бракуется межканальной
    проверкой (допуск 30 дБ) — Gain Match измеряет большой ΔG как результат."""
    hw = ScriptedHW(gain=(1.0, 0.25))   # R тише на 12 дБ — вдвое больше, чем
    rig = make_rig(hw, edge_protect=True)   # в старом тесте, но < 30 дБ
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got_r = demod_amp_dbfs(wins[0][:, 1], FS, 1000.0)
    assert abs(got_r - (TONE_DBFS - 12.0)) < 0.5


def test_tail_cut_survives():
    """Потеря части внешнего хвоста (0.5 с из защитных 0.6) с включённой
    защитой: измерительное окно не затронуто, измерение проходит."""
    hw = ScriptedHW(tail_cut_s=0.5)
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    wins, warns = rig.run([([(k, a)], [(k, a)], None)])
    got = demod_amp_dbfs(wins[0][:, 1], FS, 1000.0)
    assert abs(got - TONE_DBFS) < 0.5


def test_tail_cut_excessive_fails():
    """Обрезка 1.0 с > защитных 0.6+0.05: захват короче программы →
    явный отказ «захват оборвался»."""
    hw = ScriptedHW(tail_cut_s=1.0)
    rig = make_rig(hw, edge_protect=True)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "короче" in str(exc) or "обрыв" in str(exc), \
            f"неожиданная причина: {exc}"
        return
    raise AssertionError("обрезка 1.0 с не привела к отказу")


def test_tail_cut_unprotected_fails():
    """Без защиты обрезка 0.3 с съедает запас сегмента (0.05) и заходит в
    измерительное окно → явный отказ (значение защиты наглядно)."""
    hw = ScriptedHW(tail_cut_s=0.3)
    rig = make_rig(hw, edge_protect=False)
    (k, a), _ = tone_seg(rig, 1000.0, TONE_DBFS)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError:
        return
    raise AssertionError("без защиты обрезка 0.3 с прошла незамеченной")


def test_edge_protect_default_off():
    """Структурная проверка (ревизия v2.11): Rig без передачи параметра
    создаётся с ВЫКЛЮЧЕННОЙ edge-защитой (явная политика включения)."""
    rig = m.Rig(ScriptedHW(), calib.Calibration(), fs=FS)
    assert rig.edge_protect_enabled is False, \
        "Rig() по умолчанию должен создавать БЕЗ edge-защиты (v2.11)"


def test_gain_match_series_delta_g():
    """Фактический расчёт Gain Match (ревизия v2.11, п.5): двухшаговая
    серия — loopback-референс, затем DUT с R на 6.02 дБ тише; проверяются
    уровни G-компонент (L −30/R −36.02) и ΔG после вычитания коррекции."""
    from valvebench import gainmatch
    freqs = (987.0, 1123.0)
    hw_lb = ScriptedHW()
    rig_lb = make_rig(hw_lb, edge_protect=True)
    lb = gainmatch.measure_series(rig_lb, frequencies=freqs, level_dbfs=-30.0,
                                  repeats=2, purpose="loopback")
    assert lb["purpose"] == "loopback"
    for row in lb["rows"]:
        assert abs(row["delta_adc_db"]) < 0.1, \
            f"loopback ΔG на {row['requested_frequency_hz']} Гц: {row['delta_adc_db']:.3f}"
    ref = gainmatch.build_loopback_reference(lb)

    hw_dut = ScriptedHW(gain=(1.0, 0.5))   # R тише ровно на 20lg(0.5)=6.02 дБ
    rig_dut = make_rig(hw_dut, edge_protect=True)
    dut = gainmatch.measure_series(rig_dut, frequencies=freqs, level_dbfs=-30.0,
                                   repeats=1, purpose="dut", reference=ref)
    for row in dut["rows"]:
        f = row["requested_frequency_hz"]
        lv_l = row["channels"]["L"]["level_dbfs"]
        lv_r = row["channels"]["R"]["level_dbfs"]
        assert abs(lv_l - (-30.0)) < 0.3, f"{f} Гц: уровень L {lv_l:.2f}"
        assert abs(lv_r - (-36.02)) < 0.3, f"{f} Гц: уровень R {lv_r:.2f}"
        assert abs(row["delta_adc_db"] - 6.02) < 0.3, \
            f"{f} Гц: ΔG(L−R) {row['delta_adc_db']:.3f}"
        assert row["delta_gain_db"] is not None, "ΔG после коррекции не вычислен"
        assert abs(row["delta_gain_db"] - 6.02) < 0.3, \
            f"{f} Гц: скорректированный ΔG {row['delta_gain_db']:.3f}"


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            fails += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:
            fails += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\nRig.run() regressions: {len(tests) - fails}/{len(tests)} PASS")
    sys.exit(1 if fails else 0)
