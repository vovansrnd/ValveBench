"""Накопление пиков по свипу «с пластинки»: точность, устойчивость к щелчкам, переходное затухание, сохранение."""
import os, sys, math, json, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from valvebench import peakhold as ph, dsp
ok = 0
def check(name, cond, info=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + name + (f": {info}" if info else ""))
    if not cond: raise SystemExit(1)
    ok += 1

fs = 96000; T = 60.0; f0, f1 = 20.0, 20000.0
rng = np.random.default_rng(42)
t = np.arange(int(T * fs)) / fs
k = f1 / f0
phase = 2 * np.pi * f0 * T / math.log(k) * (k ** (t / T) - 1.0)
finst = f0 * k ** (t / T)
def fr_db(f):      # «истинная» АЧХ тракта: подъём на ВЧ и спад на НЧ, дБ
    return -20.0 + 3.0 * np.log2(np.maximum(f, 20.0) / 1000.0) * 0.6 - 4.0 * (np.maximum(f, 20) < 60)
def xt_db(f):      # «истинное» переходное затухание: хуже на ВЧ
    return -42.0 + 8.0 * np.log2(np.maximum(f, 20.0) / 1000.0) * 0.5
A = 10 ** (fr_db(finst) / 20)
L = A * np.sin(phase)
R = A * 10 ** (xt_db(finst) / 20) * np.sin(phase + 0.7)
# щелчки пластинки (широкополосные импульсы) и рокот
for pos in rng.integers(0, t.size - 200, 60):
    L[pos:pos + 4] += rng.uniform(0.02, 0.08) * rng.choice([-1, 1]); R[pos:pos + 4] += rng.uniform(0.02, 0.08) * rng.choice([-1, 1])
rumble = np.convolve(rng.standard_normal(t.size), np.ones(2000) / 2000, mode="same") * 0.05
rumble_r = np.convolve(rng.standard_normal(t.size), np.ones(2000) / 2000, mode="same") * 0.035
L += rumble; R += rumble_r      # рокот в каналах независим (общая часть была бы неотличима от утечки)
X = np.column_stack([L, R])

n = 32768; hop = int(0.30 * fs)
acc = ph.PeakHoldAccumulator(per_octave=6)
t0 = time.perf_counter(); frames = 0
for s in range(0, t.size - n, hop):
    r = ph.rta_job(X[s:s + n], fs, n, per_octave=6)
    acc.update(r["bands"], r["tone"]); frames += 1
print(f"{frames} кадров, {(time.perf_counter()-t0)/frames*1000:.0f} мс/кадр")
c = acc.centers
truth = fr_db(c)
sel = (c > 60) & (c < 15000)
tone = acc.tone_hold()
bands = acc.bands
err_tone = tone[0][sel] - truth[sel]
err_bands = bands[0][sel] - truth[sel]
m = np.isfinite(err_tone)
check("трекинг тона: ведущий канал совпадает с истинной АЧХ (|ошибка| ≤ 0.8 дБ, 60 Гц–15 кГц)",
      m.sum() >= 0.9 * sel.sum() and np.max(np.abs(err_tone[m])) <= 0.8,
      f"макс {np.max(np.abs(err_tone[m])):.2f} дБ, ячеек {m.sum()}/{sel.sum()}")
truth_r = fr_db(c) + xt_db(c)
err_r_tone = tone[1][sel] - truth_r[sel]; err_r_bands = bands[1][sel] - truth_r[sel]
mt = np.isfinite(err_r_tone)
within = np.mean(np.abs(err_r_tone[mt]) <= 2.0)
check("слабый (чужой) канал: когерентный трекинг — средняя |ошибка| < 0.8 дБ и ≥90% ячеек в пределах 2 дБ; "
      "режим «полосы» смещён вверх (рокот/щелчки) и хуже",
      np.mean(np.abs(err_r_tone[mt])) < 0.8 and within >= 0.9
      and np.nanmax(err_r_bands) > 6.0 and np.nanmean(np.abs(err_r_bands)) > 1.5 * np.mean(np.abs(err_r_tone[mt])),
      f"трекинг: среднее {np.mean(np.abs(err_r_tone[mt])):.2f} дБ, в пределах 2 дБ {within:.0%}; "
      f"полосы: среднее {np.nanmean(np.abs(err_r_bands)):.2f} дБ, максимум {np.nanmax(err_r_bands):+.1f} дБ")
d, xt = ph.split_driven(tone)
xt_true = xt_db(c) 
mx = np.isfinite(xt) & sel
check("переходное затухание по трекингу: ведущий канал L определён, на 100 Гц–15 кГц ошибка ≤ 3 дБ",
      d == 0 and np.max(np.abs((xt - xt_true)[mx & (c > 100)])) <= 3.0,
      f"макс ошибка {np.max(np.abs((xt - xt_true)[mx & (c > 100)])):.2f} дБ")
cov, tot = acc.coverage()
check("охват свипа: ≥95% ячеек", cov >= 0.95 * tot, f"{cov}/{tot}")
rel = ph.relative_levels(c, tone)
i1k = int(np.argmin(np.abs(np.log(c / 1000.0))))
check("нормировка: ведущий канал на 1 кГц — 0 дБ, слабый (утечка) сохраняет отступ от ведущего",
      abs(rel[0][i1k]) < 0.01 and abs(rel[1][i1k] - (tone[1][i1k] - tone[0][i1k])) < 0.01 and rel[1][i1k] < -30,
      f"L {rel[0][i1k]:+.3f}, R {rel[1][i1k]:+.1f} дБ")
st = np.array([[-20.0, -21.0], [-21.0, -22.0]]).T.copy(); st = np.vstack([[-20.0, -21.0, -22.0], [-20.5, -21.5, -22.5]])
rs = ph.relative_levels(np.array([900.0, 1000.0, 1100.0]), st)
check("стерео-свип (оба ведущие): каждый канал на свой ноль", abs(rs[0][1]) < 1e-9 and abs(rs[1][1]) < 1e-9)
# сброс
acc2 = ph.PeakHoldAccumulator(6); acc2.update(None, {"f": 1000.0, "lvl": np.array([-20.0, -60.0]), "driven": 0, "num": 0.001 + 0j, "den": 1.0}); acc2.reset()
check("сброс очищает удержание", np.all(np.isnan(acc2.tone_hold())) and np.all(np.isnan(acc2.bands)) and acc2.frames == 0)
# сохранение/загрузка/CSV
o1 = acc.snapshot("AT120", ph.PALETTE[0], "tone", fs)
o2 = acc.snapshot("MP100 \"тест\"", ph.PALETTE[1], "tone", fs); o2.levels = o2.levels - 1.5; o2.visible = False
data = json.loads(json.dumps(ph.overlays_to_json([o1, o2])))
back = ph.overlays_from_json(data)
check("JSON: кривые переживают сохранение (NaN ↔ null, цвет, видимость)",
      len(back) == 2 and np.allclose(back[0].levels, o1.levels, equal_nan=True, atol=1e-3) and back[1].visible is False
      and back[0].color == tuple(ph.PALETTE[0]))
data["overlays"].insert(0, {"name": "битая"})
check("повреждённая запись пропускается, остальные не теряются", len(ph.overlays_from_json(data)) == 2)
csv_all = ph.overlays_csv([o1, o2], normalize=True).splitlines()
meta_lines = [l for l in csv_all if l.startswith("#")]
csv = [l for l in csv_all if not l.startswith("#")]
check("CSV: метаданные контекста в «#»-строках, заголовок и строки по числу полос",
      len(meta_lines) == 2 and "correction=raw" in meta_lines[0] and csv[0].startswith('"freq_hz"')
      and len(csv) == 1 + acc.centers.size and "xtalk" in csv[0])

# ---- dev3: НЧ-точность зависит от длительности окна, а не от числа отсчётов
def lf_error(fs_, secs, T_=40.0):
    n_ = ph.window_samples(fs_, secs)
    tt_ = np.arange(int(T_ * fs_)) / fs_; k_ = 1000.0
    phs = 2 * np.pi * 20 * T_ / math.log(k_) * (k_ ** (tt_ / T_) - 1)
    Xf = np.column_stack([0.1 * np.sin(phs), 1e-9 * np.sin(phs)])
    ac = ph.PeakHoldAccumulator(12)
    for s0 in range(0, Xf.shape[0] - n_, int(0.3 * fs_)):
        r_ = ph.rta_job(Xf[s0:s0 + n_], fs_, n_, 12); ac.update(r_["bands"], r_["tone"])
    cc = ac.centers; th_ = ac.tone_hold()[0]; m_ = (cc >= 20) & (cc < 40) & np.isfinite(th_)
    return float(np.mean(th_[m_] - 20 * math.log10(0.1))), int(m_.sum())
errs = {(fs_, sec): lf_error(fs_, sec) for fs_ in (96000, 192000) for sec in (0.35, 0.7)}
check("НЧ (20–40 Гц): окно 0.35 и 0.7 с при 96 и 192 кГц — средняя ошибка ≤ 0.1 дБ (было −2.4 дБ при 192 кГц/64k)",
      all(abs(e) <= 0.1 and cnt >= 4 for e, cnt in errs.values()),
      "; ".join(f"{fs_ // 1000}k/{sec} с: {e:+.3f} дБ" for (fs_, sec), (e, _c) in errs.items()))
check("окно задаётся временем: 0.7 с = 65536 отсчётов при 96 кГц и 131072 при 192 кГц",
      ph.window_samples(96000, 0.7) == 65536 and ph.window_samples(192000, 0.7) == 131072)
check("предел НЧ окна: 6·Δf (0.35 с @192 кГц → 17.6 Гц)", abs(ph.lf_limit_hz(192000, 65536) - 17.58) < 0.05)

# ---- dev3: одиночные аномалии отсчётов почти не влияют на накопление
def glitch_effect(gdb, chan):
    fs_ = 96000; T_ = 30.0; n_ = 65536; rg = np.random.default_rng(12)
    tt_ = np.arange(int(T_ * fs_)) / fs_; k_ = 1000.0
    phs = 2 * np.pi * 20 * T_ / math.log(k_) * (k_ ** (tt_ / T_) - 1)
    Lg = 0.1 * np.sin(phs) + 1e-6 * rg.standard_normal(tt_.size)
    Rg = 0.1 * 10 ** (-45 / 20) * np.sin(phs + 0.4) + 2e-6 * rg.standard_normal(tt_.size)
    for pos in rg.integers(1000, tt_.size - 1000, 20):
        (Lg if chan == "L" else Rg)[pos] += 10 ** (gdb / 20) * rg.choice([-1, 1])
    ac = ph.PeakHoldAccumulator(12)
    for s0 in range(0, tt_.size - n_, int(0.3 * fs_)):
        r_ = ph.rta_job(np.column_stack([Lg[s0:s0 + n_], Rg[s0:s0 + n_]]), fs_, n_, 12); ac.update(r_["bands"], r_["tone"])
    cc = ac.centers; sel_ = (cc > 60) & (cc < 15000); th_ = ac.tone_hold()
    return (float(np.nanmax(np.abs(th_[0][sel_] - 20 * math.log10(0.1)))),
            float(np.nanmax(np.abs(th_[1][sel_] - (20 * math.log10(0.1) - 45)))))
eL, eR = glitch_effect(-20.0, "R")
check("20 одиночных выбросов −20 dBFS в слабом канале: ошибка АЧХ ведущего и переходного затухания ≤ 0.3 дБ (трекинг тона)",
      eL <= 0.3 and eR <= 0.3, f"ведущий {eL:.2f} дБ, слабый {eR:.2f} дБ")
eL2, eR2 = glitch_effect(-20.0, "L")
check("то же в ведущем канале", eL2 <= 0.3 and eR2 <= 0.3, f"ведущий {eL2:.2f} дБ, слабый {eR2:.2f} дБ")

# ---- dev3: поправка АЧХ карты
prof = {"kind": "frequency_response_calibration", "channels": {
    "L": {"frequency_hz": [20.0, 1000.0, 20000.0], "fr_norm_db": [-1.0, 0.0, -3.0]},
    "R": {"frequency_hz": [20.0, 1000.0, 20000.0], "fr_norm_db": [-1.0, 0.0, -3.0]}}}
cv = ph.correction_curve(prof, acc.centers)
i20k = int(np.argmin(np.abs(np.log(acc.centers / 15000.0))))
check("поправка карты: на 1 кГц 0 дБ, на ВЧ — по профилю, вне диапазона 0",
      cv is not None and abs(cv[0][int(np.argmin(np.abs(np.log(acc.centers / 1000.0))))]) < 0.05 and cv[0][i20k] < -1.5,
      f"15 кГц: {cv[0][i20k]:+.2f} дБ")
ovm = acc.snapshot("м", ph.PALETTE[0], "tone", fs, card="usb-emu", mixer="abc123", corr_db=cv, corr_note="петля")
back2 = ph.overlays_from_json(json.loads(json.dumps(ph.overlays_to_json([ovm]))))[0]
check("метаданные кривой (карта, Fs, микшер, поправка) переживают JSON",
      back2.card == "usb-emu" and back2.mixer == "abc123" and back2.corr_db is not None
      and np.allclose(back2.corr_db, cv, atol=1e-3) and back2.fs == fs)
print(f"\n{ok} проверок пройдено")
