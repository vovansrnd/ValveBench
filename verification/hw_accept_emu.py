#!/usr/bin/env python3
"""Аппаратная приёмка ЭТАП A: E-MU 0404 USB, аналоговый loopback.

ТЗ §11: обнаружение, открытие, уровни, ΔG, частоты, перегрузка, длительный
захват, XRUN, TARE-маршрут. Уровень выхода умеренный (−6 dBFS цифры);
фактические параметры потока — из /proc/asound, не из настроек UI.
"""
import fcntl
import json
import math
import os
import subprocess
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from valvebench import gainmatch
from valvebench.calib import Calibration
from valvebench.measure import Rig
from valvebench.backend import SimChannel  # noqa: F401

EMU_USBID = "041e:3f04"
# автодетект карты E-MU по usbid
def find_emu_card():
    for c in os.listdir("/proc/asound"):
        if c.startswith("card"):
            try:
                usbid = open(f"/proc/asound/{c}/usbid").read().strip()
                if usbid == EMU_USBID:
                    return f"hw:{c[4:]},0"
            except OSError:
                pass
    return None

EMU_HW = find_emu_card() or "hw:3,0"
EMU_USBID = "041e:3f04"
FS = 48000
LEVEL = -6.0
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("\n      " + detail if detail else ""))


def usbid_ok():
    try:
        return open("/proc/asound/" + EMU_HW.split(",")[0].replace("hw:", "card") + "/usbid").read().strip() == EMU_USBID
    except OSError:
        return False


def hw_params_snapshot(dev="pcm0p"):
    path = f"/proc/asound/{EMU_HW.split(",")[0].replace("hw:", "card")}/{dev}/sub0/hw_params"
    try:
        return open(path).read().strip().replace("\n", ", ")
    except OSError:
        return "closed"


def parse_raw(path, fs):
    raw = open(path, "rb").read()
    n = len(raw) // 6
    b = np.frombuffer(raw[: n * 6], dtype=np.uint8).reshape(n, 6)
    x = np.zeros((n, 2))
    for c in (0, 1):
        v = b[:, 3 * c:3 * c + 3].copy().astype(np.int32)
        v = v[:, 0] | (v[:, 1] << 8) | (v[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x[:, c] = v / float(1 << 23)
    return x


def float_to_s24le(x):
    """float [-1,1] → байты S24_3LE (3 байта на сэмпл, little endian)."""
    v = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    iv = np.round(v * float(1 << 23)).astype(np.int64)
    iv = np.clip(iv, -(1 << 23), (1 << 23) - 1).astype("<i4")
    b3 = np.column_stack([iv & 0xFF, (iv >> 8) & 0xFF, (iv >> 16) & 0xFF]).astype(np.uint8)
    return b3.tobytes()


def capture_play(play_frames, rec_seconds, fs=FS, play_dev=EMU_HW, rec_dev=EMU_HW):
    raw = "/tmp/hw_probe_play.raw"
    with open(raw, "wb") as fh:
        fh.write(float_to_s24le(play_frames))
    cap = "/tmp/hw_probe_cap.raw"
    arecord_cmd = ["arecord", "-D", rec_dev, "-f", "S24_3LE", "-r", str(fs),
                   "-c", "2", "-d", str(rec_seconds), "-t", "raw", cap]
    aplay_cmd = ["aplay", "-D", play_dev, "-f", "S24_3LE", "-r", str(fs),
                 "-c", "2", "-t", "raw", raw]
    proc_rec = subprocess.Popen(arecord_cmd, stderr=subprocess.PIPE)
    time.sleep(0.4)
    proc_play = subprocess.Popen(aplay_cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    proc_play.wait()
    out, err = proc_rec.communicate(timeout=rec_seconds + 10)
    xrun_lines = [ln for ln in err.decode("utf-8", "replace").splitlines()
                  if "xrun" in ln.lower()]
    return parse_raw(cap, fs), xrun_lines


def main():
    t_start = time.time()
    # --- 1. обнаружение и идентификация
    check("HW-ID: E-MU 0404 USB на месте (usbid)", usbid_ok(), EMU_USBID)

    # --- 2. фактические параметры потока (открываем и читаем hw_params)
    proc = subprocess.Popen(["aplay", "-D", EMU_HW, "-f", "S24_3LE", "-r", str(FS),
                             "-c", "2", "-d", "2", "/dev/zero"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    snap_play = hw_params_snapshot("pcm0p")
    proc.wait()
    cap_proc = subprocess.Popen(["arecord", "-D", EMU_HW, "-f", "S24_3LE", "-r", str(FS),
                                 "-c", "2", "-d", "2", "/dev/null"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    snap_cap = hw_params_snapshot("pcm0c")
    cap_proc.wait()
    check("HW-PARAM: playback-поток согласован", "S24_3LE" in snap_play and str(FS) in snap_play,
          f"play: {snap_play}")
    check("HW-PARAM: capture-поток согласован", "S24_3LE" in snap_cap and str(FS) in snap_cap,
          f"cap: {snap_cap}")

    # --- 3. шумовой пол (без тона): опорный уровень тракта
    noise_rec, _ = capture_play(np.zeros((int(FS * 0.5), 2)), 4)
    seg = noise_rec[FS:FS+FS, :]  # середина тишины (после прогрева, до конца игры)
    nf_l = 20 * math.log10(max(float(np.sqrt(np.mean(seg[:, 0] ** 2))) * math.sqrt(2), 1e-12))
    nf_r = 20 * math.log10(max(float(np.sqrt(np.mean(seg[:, 1] ** 2))) * math.sqrt(2), 1e-12))
    print(f"      шумовой пол тракта (с трактом усиления): L {nf_l:.1f}, R {nf_r:.1f} dBFS")

    # --- 4. тон 1 кГц −6 dBFS в оба канала: уровни, ΔG, XRUN
    t = np.arange(FS * 2) / FS
    tone = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    frames = np.column_stack([tone, tone])
    rec, xruns = capture_play(frames, 4)
    seg = rec[int(0.6 * FS):int(2.3 * FS), :]
    lv = []
    for c in (0, 1):
        rms = float(np.sqrt(np.mean(seg[:, c] ** 2)))
        lv.append(20 * math.log10(max(rms * math.sqrt(2), 1e-12)))
    clip = float(np.max(np.abs(seg))) >= 0.98
    delta_g = lv[0] - lv[1]
    check("HW-TONE: тон 1 кГц проходит в оба канала, клипа нет",
          min(lv) > nf_l + 10 and not clip,
          f"L {lv[0]:.1f}, R {lv[1]:.1f} dBFS; клип={clip}; шумовой пол {nf_l:.1f}")
    check("HW-XRUN0: XRUN при 4-с измерении не зарегистрированы", not xruns,
          f"{xruns if xruns else 'нет сообщений ALSA'}")

    # --- 5. Gain Match series (100/1k/10 кГц) через measure_series
    from valvebench.backend import Backend

    class EmuHw(Backend):
        name = "alsa-hw"

        def __init__(self):
            self.hw = EMU_HW
            self.notes = []
            self.fs = FS

        def open(self, fs):
            self.fs = int(fs)

        def play_record(self, out, tail=0.8):
            out = np.asarray(out, dtype=np.float64)
            if out.ndim == 1:
                out = np.stack([out, out], axis=1)
            n = out.shape[0]
            rec_s = n / self.fs + tail + 0.5
            raw_play = "/tmp/hw_gm_play.raw"
            cap = "/tmp/hw_gm_cap.raw"
            with open(raw_play, "wb") as fh:
                fh.write(float_to_s24le(out))
            rec = subprocess.Popen(["arecord", "-D", EMU_HW, "-f", "S24_3LE",
                                    "-r", str(self.fs), "-c", "2", "-d", str(int(math.ceil(rec_s))),
                                    "-t", "raw", cap], stderr=subprocess.PIPE)
            time.sleep(0.4)
            play = subprocess.run(["aplay", "-D", EMU_HW, "-q", "-f", "S24_3LE",
                                   "-r", str(self.fs), "-c", "2", "-t", "raw", raw_play],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _, out_err = rec.communicate(timeout=rec_s + 10)
            notes = [ln for ln in out_err.decode("utf-8", "replace").splitlines()
                     if "xrun" in ln.lower()]
            self.notes = notes
            data = open(cap, "rb").read()
            return parse_raw(cap, self.fs)

        # live-интерфейс не нужен
        def live_start(self): raise NotImplementedError
        def live_stop(self): pass
        def live_set_loop(self, p): raise NotImplementedError
        def live_recent(self, s): raise NotImplementedError

    be = EmuHw()
    be.open(FS)
    rig = Rig(be, Calibration(), fs=FS, blocks=2, settle=0.3)
    rig.open()
    res = gainmatch.measure_series(rig, frequencies=(100.0, 1000.0, 10000.0),
                                   level_dbfs=LEVEL, repeats=3, purpose="loopback")
    for row in res["rows"]:
        print(f"      {row['frequency_hz']:.0f} Гц: L={row['channels']['L']['level_dbfs']:.2f} "
              f"R={row['channels']['R']['level_dbfs']:.2f} dBFS "
              f"(L−R={row['delta_adc_db']:+.2f}), C/R L={row['channels']['L']['carrier_residual_db']:.0f}")
    deltas = [row["delta_adc_db"] for row in res["rows"]]
    spreads = [row["delta_output_spread_db"] for row in res["rows"]]
    crs = [row["channels"]["L"]["carrier_residual_db"] for row in res["rows"]]
    valid = [row["valid"] and row["repeat_stable"] for row in res["rows"]]
    text = gainmatch.text_series(res)
    check("HW-GM: серия 100/1к/10к валидна на реальном тракте", all(valid),
          f"ΔADC={['%.3f' % d for d in deltas]}; spread={['%.3f' % s for s in spreads]}; "
          f"C/R={['%.0f' % c for c in crs]}; XRUN-заметки={len(be.notes)}")
    check("HW-GM-DELTA: |ΔADC| loopback < 0.1 дБ", max(abs(d) for d in deltas) < 0.1,
          f"макс |Δ| = {max(abs(d) for d in deltas):.3f} дБ")

    # --- 6. известная разница +0.5 дБ в цифровом канале L → ΔG сдвигается на +0.5
    be2 = EmuHw()
    be2.open(FS)
    rig2 = Rig(be2, Calibration(), fs=FS, blocks=2, settle=0.3)
    rig2.open()
    # измеряем с цифровым +0.5 дБ на L (разница уровней измеряется по записи —
    # series требует ≥2 частот, здесь достаточно прямого измерения)
    t = np.arange(FS * 2) / FS
    tone_l = 10 ** ((LEVEL + 0.5) / 20) * np.sin(2 * np.pi * 1000.0 * t)
    tone_r = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    frames = np.column_stack([tone_l, tone_r])
    rec2, _ = capture_play(frames, 4)
    seg2 = rec2[int(0.6 * FS):int(2.3 * FS), :]   # целиком внутри 2-с тона (0.4–2.4 с)
    lv2 = [20 * math.log10(max(float(np.sqrt(np.mean(seg2[:, c] ** 2))) * math.sqrt(2), 1e-12))
           for c in (0, 1)]
    dg_shift = lv2[0] - lv2[1]
    check("HW-SHIFT: известная цифровая разница +0.5 дБ измерена как +0.5±0.05 дБ",
          abs(dg_shift - 0.5) < 0.05, f"измеренная разница каналов = {dg_shift:+.3f} дБ")

    # --- 7. перегрузка: +6 дБ к уровню → клип должен быть виден
    t = np.arange(FS * 2) / FS
    hot = 10 ** ((LEVEL + 12.0) / 20) * np.sin(2 * np.pi * 1000.0 * t)
    frames_hot = np.column_stack([hot, hot])
    rec3, _ = capture_play(frames_hot, 4)
    seg3 = rec3[int(0.6 * FS):int(2.3 * FS), :]
    peak3 = float(np.max(np.abs(seg3)))
    clipped = float(np.sum(np.abs(seg3) >= 0.985))
    check("HW-CLIP: при +12 дБ к уровню видна перегрузка АЦП (сэмплы у предела)",
          peak3 >= 0.9 and clipped > 0,
          f"peak={peak3:.3f}, сэмплов ≥0.985: {clipped}")

    # --- 8. длительный захват 60 с: дрейф и XRUN
    t = np.arange(FS * 60) / FS
    tone = 10 ** (LEVEL / 20) * np.sin(2 * np.pi * 1000.0 * t)
    frames = np.column_stack([tone, tone])
    rec60, xruns60 = capture_play(frames, 62)
    win = FS * 10
    drift = []
    for s in range(0, len(rec60) - win, win):
        seg = rec60[s:s + win, :]
        rms = float(np.sqrt(np.mean(seg[:, 0] ** 2)))
        drift.append(20 * math.log10(max(rms * math.sqrt(2), 1e-12)))
    span = max(drift) - min(drift)
    check("HW-LONG: 60 с захвата — дрейф уровня < 0.1 дБ, без XRUN",
          span < 0.1 and not xruns60,
          f"дрейф уровня L = {span:.3f} дБ; XRUN={xruns60 if xruns60 else 'нет'}")

    # --- 9. TARE-маршрут: обнуление при текущем тракте, смена частоты — сброс
    tr = gainmatch.GainTracker()
    r1k = rec[int(0.7 * FS):int(0.7 * FS) + int(0.6 * FS), :]
    ctx1k = {"freq": 1000.0, "level_dbfs": LEVEL, "fs": FS, "route": 0,
             "device_key": "usb:041e:3f04", "mixer_hash": "emu", "cal_sig": "none"}
    st1 = tr.update(r1k, FS, 1000.0, LEVEL, 0, None, now=0.0, ctx=ctx1k)
    tr.tare(ctx=ctx1k)
    r10k = rec[int(0.7 * FS):int(0.7 * FS) + int(0.6 * FS), :]
    st2 = tr.update(r10k, FS, 10000.0, LEVEL, 0, None, now=1.0,
                    ctx=dict(ctx1k, freq=10000.0))
    check("HW-TARE: смена частоты на реальном тракте сбрасывает ноль с причиной",
          st2["tare_db"] == 0.0 and st2.get("tare_reset_reason") is not None,
          f"reason={st2.get('tare_reset_reason')}")

    print(f"\nВремя выполнения: {time.time() - t_start:.0f} с")
    npass = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"ИТОГ ЭТАПА A (E-MU loopback): {npass}/{len(RESULTS)}")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hw-accept-emu-results.json"), "w", encoding="utf-8") as fh:
        json.dump({"results": RESULTS,
                   "hw_params": {"play": snap_play, "cap": snap_cap},
                   "levels_1k": lv, "noise_floor": [nf_l, nf_r],
                   "delta_g_loopback": deltas, "drift_60s_db": drift,
                   "xruns": {"short": xruns, "long": xruns60}}, fh,
                  ensure_ascii=False, indent=1, default=str)
    print("JSON: hw-accept-emu-results.json")
    sys.exit(0 if npass == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
