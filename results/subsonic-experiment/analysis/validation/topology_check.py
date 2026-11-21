#!/usr/bin/env python3
"""Проверка математики сабсоник-цепи против таблицы из инструкции.

Топологии (источник SRPP считаем низкоомным, ~0 Ом):
  H0 bypass:    src -> C1(2.2u) -> nodeA (100k -> gnd) -> RCA (10k -> gnd)
                100k висит прямо на узле A, нагрузка 10k параллельно.
  H1 filtered:  src -> C1(2.2u) -> nodeA (100k -> gnd) -> C2(new) -> RCA (10k)
                100k ДО нового конденсатора (вариант из инструкции).
  H1b alt:      src -> C1(2.2u) -> nodeA -> C2(new) -> nodeB (100k -> gnd) -> RCA (10k)
                100k ПОСЛЕ нового конденсатора (на стороне RCA).
"""
import math

def cap(z_ohm: float, c_farad: float) -> complex:
    return 1.0 / (1j * 2 * math.pi * z_ohm * c_farad)

def parallel(*zs: complex) -> complex:
    out = 1.0 / sum(1.0 / z for z in zs)
    return out

def db(x: float) -> float:
    return 20.0 * math.log10(abs(x))

R_MID = 100_000.0
C1 = 2.2e-6

def h0(f: float, r_load: float) -> complex:
    z_a = parallel(R_MID, r_load)
    return z_a / (cap(f, C1) + z_a)

def h1_exact(f: float, r_load: float, c2: float) -> complex:
    # 100k до нового конденсатора: A = 100k || (Zc2 + Rload)
    z_branch = cap(f, c2) + complex(r_load, 0)
    z_a = parallel(complex(R_MID, 0), z_branch)
    return (z_a / (cap(f, C1) + z_a)) * (complex(r_load, 0) / (complex(r_load, 0) + cap(f, c2)))

def h1b_exact(f: float, r_load: float, c2: float) -> complex:
    # 100k после нового конденсатора: B = (100k || Rload)
    z_b = parallel(complex(R_MID, 0), complex(r_load, 0))
    z_a = z_b + cap(f, c2)              # узел A: вся правая ветвь
    return (z_a / (cap(f, C1) + z_a)) * (z_b / (z_b + cap(f, c2)))

FREQS = (0.55, 2.0, 3.0, 5.0, 8.0, 10.0, 12.5, 15.0, 20.0, 25.0, 31.5, 40.0, 50.0, 100.0)
CLAIMED = {  # таблица из инструкции для варианта "100k до C2"
    0.55: -30.0, 5.0: -10.0, 10.0: -5.0, 15.0: -3.0, 20.0: -1.9, 30.0: -1.0,
}

print("=== Вариант 1: 100k ДО нового C2=2.2u, нагрузка 10k ===")
print(f"{'f, Гц':>7} | {'H1-H0, дБ':>10} | {'заявлено':>9} | {'H1 сам, дБ':>10}")
for f in FREQS:
    delta = db(h1_exact(f, 10_000.0, 2.2e-6)) - db(h0(f, 10_000.0))
    claim = CLAIMED.get(f)
    print(f"{f:7.2f} | {delta:10.2f} | {('%+.1f' % claim) if claim is not None else '—':>9} | "
          f"{db(h1_exact(f, 10_000.0, 2.2e-6)):10.2f}")

print("\n=== Вариант 2 (alt): 100k ПОСЛЕ нового C2, нагрузка 10k ===")
for f in (5.0, 10.0, 15.0, 20.0, 30.0):
    delta = db(h1b_exact(f, 10_000.0, 2.2e-6)) - db(h0(f, 10_000.0))
    print(f"{f:7.2f} | {delta:10.2f}")

print("\n=== Ключевые частоты среза ===")
def find_fc(func, r_load, c2):
    # бинарь по уровню -3 дБ относительно ВЧ-плато (база = db на 1000 Гц)
    base = db(func(1000.0, r_load, c2)) if c2 else db(func(1000.0, r_load))
    lo, hi = 0.05, 500.0
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        val = (db(func(mid, r_load, c2)) if c2 else db(func(mid, r_load))) - base
        if val > -3.0:   # выше плато -> пересечение левее
            hi = mid
        else:
            lo = mid
    return math.sqrt(lo * hi)

fc0 = find_fc(lambda f, r: h0(f, r), 10_000.0, None)
fc1 = find_fc(lambda f, r, c: h1_exact(f, r, c), 10_000.0, 2.2e-6)
fc1b = find_fc(lambda f, r, c: h1b_exact(f, r, c), 10_000.0, 2.2e-6)
print(f"bypass (C1 в 100k||10k = {100000*10000/110000:.0f} Ом): fc = {fc0:.2f} Гц")
print(f"filtered, 100k до C2: fc(-3дБ) = {fc1:.2f} Гц; асимптота 1/(2π·10k·1.1u) = "
      f"{1/(2*math.pi*10000*1.1e-6):.2f} Гц; нижний полюс ~1/(2π·100k·4.4u) = "
      f"{1/(2*math.pi*100000*4.4e-6):.3f} Гц")
print(f"filtered, 100k после C2 (alt): fc(-3дБ) = {fc1b:.2f} Гц; асимптота 1/(2π·9.09k·1.17u) = "
      f"{1/(2*math.pi*(100000*10000/110000)*2.2*2.5/4.7*1e-6):.2f} Гц")

print("\n=== Оценка номиналов будущего переключателя (для справки) ===")
# Цель: асимптотический срез ~15 Гц на нагрузку R_in, 100k ДО C2:
# 1/(2π·R_in·(C1·C2/(C1+C2))) = 15
for r_in, claimed in ((10_000.0, 2.2e-6), (22_000.0, 0.62e-6), (47_000.0, 0.25e-6)):
    # решение: C_series = 1/(2π·R·15); C2 = C1·Cs/(C1-Cs)
    cs = 1.0 / (2 * math.pi * r_in * 15.0)
    c2 = C1 * cs / (C1 - cs) if cs < C1 else float("nan")
    print(f"R_in={r_in/1000:.0f}k: C_series={cs*1e6:.3f}u -> C2={c2*1e6:.2f}u (в инструкции {claimed*1e6:.2f}u)")
print("— если 100k ПОСЛЕ C2, нагрузка = 100k||R_in, то:")
for r_in, claimed in ((10_000.0, 2.5e-6), (22_000.0, 0.82e-6), (47_000.0, 0.39e-6)):
    r_eff = 1.0 / (1.0 / R_MID + 1.0 / r_in)
    cs = 1.0 / (2 * math.pi * r_eff * 15.0)
    c2 = C1 * cs / (C1 - cs) if cs < C1 else float("nan")
    print(f"R_in={r_in/1000:.0f}k: R_eff={r_eff:.0f} Ом, C_series={cs*1e6:.3f}u -> C2={c2*1e6:.2f}u "
          f"(в инструкции {claimed*1e6:.2f}u)")
