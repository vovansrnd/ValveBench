"""Headless smoke for the separate-process analysis worker (no GTK/audio hardware)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from valvebench.analysis import AnalysisWorker
from valvebench.peakhold import rta_job


def main() -> int:
    worker = AnalysisWorker()
    try:
        worker.warm_up()
        fs, n = 48000, 16384
        t = np.arange(n, dtype=np.float64) / fs
        x = np.column_stack((0.1 * np.sin(2 * np.pi * 997.0 * t),
                             0.08 * np.sin(2 * np.pi * 997.0 * t + 0.1)))
        if not worker.submit(rta_job, x, fs, n, 12, 20.0, 20000.0, tag={"smoke": True}):
            raise AssertionError("worker rejected the first task")
        deadline = time.monotonic() + 30.0
        result = None
        while time.monotonic() < deadline:
            result = worker.poll()
            if result is not None:
                break
            time.sleep(0.02)
        if result is None:
            raise AssertionError("analysis worker timed out")
        tag, payload, error = result
        if error:
            raise AssertionError(error)
        if worker.mode != "process":
            raise AssertionError(f"expected process mode, got {worker.mode}")
        if tag != {"smoke": True} or payload.get("tone") is None:
            raise AssertionError("missing task tag or detected tone")
        if abs(float(payload["tone"]["f"]) - 997.0) > 2.0:
            raise AssertionError(f"unexpected detected frequency: {payload['tone']['f']}")
        print(f"PASS AnalysisWorker spawn: mode={worker.mode}; tone={payload['tone']['f']:.3f} Hz; "
              f"elapsed={payload['elapsed'] * 1e3:.2f} ms")
        return 0
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
