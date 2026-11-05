"""Gain Match: узкополосное измерение уровня тона и разности усиления каналов.

Почему не RMS по 120 мс. Широкополосный RMS включает шум, сетевой фон и
гармоники лампового каскада, а окно в 120 мс не содержит целого числа периодов.
Здесь уровень тона на частоте генератора определяется методом наименьших
квадратов (cos, sin, const) по окну, кратному 20 мс (целое число периодов
сети): шум и фон не попадают в результат, а ΔG устойчива до тысячных долей дБ.
Модуль не зависит от GTK и проверяется автоматически.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

WINDOW_S = 0.5            # кратно 20 мс: целое число периодов 50 Гц (и 60 Гц)
RANGES = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)


def tone_fit(x, fs, f):
    """(пиковая амплитуда, фаза рад, rms остатка) тона частоты f в x (LSQ cos/sin/const)."""
    x = np.asarray(x, dtype=np.float64)
    t = np.arange(x.size) / float(fs)
    w = 2.0 * math.pi * f
    a = np.column_stack((np.cos(w * t), np.sin(w * t), np.ones_like(t)))
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    res = x - a @ coef
    amp = math.hypot(coef[0], coef[1])
    return amp, math.atan2(-coef[1], coef[0]), float(np.sqrt(np.mean(res * res)))


def to_db(a):
    return 20.0 * math.log10(max(a, 1e-12))


def pick_range(delta_db, tol_db):
    """Полуширина шкалы ΔG: минимальная из ряда, где стрелка остаётся в пределах 80 %."""
    need = max(abs(delta_db) / 0.8, 3.0 * tol_db)
    for r in RANGES:
        if r >= need:
            return r
    return RANGES[-1]


def classify(delta_db, tol_db):
    """'ok' внутри допуска, 'near' в пределах трёх допусков, иначе 'far'."""
    d = abs(delta_db)
    return "ok" if d <= tol_db else ("near" if d <= 3.0 * tol_db else "far")


class GainTracker:
    """Скользящая оценка ΔG = G(L) − G(R) по живому захвату."""

    def __init__(self, tau=1.5, hist_s=3.0):
        self.tau = tau
        self.hist_s = hist_s
        self.tare_db = 0.0
        self.reset()

    def reset(self):
        self.filt = None
        self.hist = deque()
        self.last_t = None
        self.last_raw = None

    def tare(self, value=None):
        """Принять текущую разность за ноль (петля или заведомо одинаковые каналы)."""
        if value is None:
            value = self.last_raw
        if value is not None:
            # "Принять текущее значение за ноль" задаёт новую опорную точку.
            # Нельзя накапливать поправку при повторном нажатии: это сдвинет
            # индикатор в противоположную сторону на уже учтённую ΔG.
            self.tare_db = float(value)
            self.reset()
        return self.tare_db

    def clear_tare(self):
        self.tare_db = 0.0
        self.reset()

    def update(self, ring, fs, f, gen_dbfs, route, cal=None, now=0.0):
        """ring — последний захват (N, 2). Возвращает словарь состояния для индикатора."""
        n = int(round(WINDOW_S * fs))
        if route != 0:
            self.reset()
            return {"valid": False, "msg": "ΔG: подай тест в оба канала (L + R)"}
        if ring is None or len(ring) < n:
            self.reset()
            return {"valid": False, "msg": "накопление данных…"}
        seg = ring[-n:]
        amps, phs, res = [], [], []
        for c in (0, 1):
            a, ph, r = tone_fit(seg[:, c], fs, f)
            amps.append(a); phs.append(ph); res.append(r)
        lv = [to_db(a / 1.0) for a in amps]               # dBFS пика синуса (0 dBFS = полный синус)
        if min(amps) < 1e-6:
            self.reset()
            return {"valid": False, "msg": "на входе нет тона: проверь кабели и частоту"}
        snr = [to_db(a / math.sqrt(2.0)) - to_db(max(r, 1e-12)) for a, r in zip(amps, res)]
        if min(snr) < 15.0:
            self.reset()
            return {"valid": False, "msg": f"недостаточный SNR для сравнения каналов ({min(snr):.1f} dB; нужно ≥15 dB)"}
        gains = None
        calibrated = False
        if cal is not None:
            try:
                g = [cal.gain_db(float(gen_dbfs), lv[c], c) for c in (0, 1)]
                if all(v is not None for v in g):
                    gains, calibrated = g, True
            except Exception:                              # старый профиль без нужных полей
                gains = None
        raw = (gains[0] - gains[1]) if calibrated else (lv[0] - lv[1])
        self.last_raw = raw
        delta = raw - self.tare_db
        # фильтр и разброс
        if self.filt is None or self.last_t is None:
            self.filt = delta
        else:
            dt = max(0.0, now - self.last_t)
            al = 1.0 - math.exp(-dt / self.tau)
            self.filt += al * (delta - self.filt)
        self.last_t = now
        self.hist.append((now, delta))
        while self.hist and now - self.hist[0][0] > self.hist_s:
            self.hist.popleft()
        vals = np.array([v for _, v in self.hist])
        sigma = float(np.std(vals)) if len(vals) > 5 else None
        dphi = math.degrees(((phs[0] - phs[1] + math.pi) % (2 * math.pi)) - math.pi)
        return {"valid": True, "delta": delta, "filt": self.filt, "sigma": sigma,
                "level_dbfs": lv, "snr_db": snr, "phase_deg": dphi,
                "gain_db": gains, "calibrated": calibrated, "tare_db": self.tare_db,
                "msg": ""}
