#!/usr/bin/env python3
"""Source-level UI contract checks; does not import GTK or claim GUI acceptance."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "valvebench" / "ui.py"
UI = UI_PATH.read_text(encoding="utf-8")
TREE = ast.parse(UI)


def class_node(name: str):
    return next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == name)


def method_text(class_name: str, method_name: str) -> str:
    cls = class_node(class_name)
    method = next(node for node in cls.body
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == method_name)
    return ast.get_source_segment(UI, method) or ""


checks = []


def check(name: str, ok: bool, detail: str = ""):
    checks.append((name, bool(ok), detail))


# One batch page, one snapshot/save action, selectable key tests.
batch = class_node("MeasurementBatchPage")
batch_text = ast.get_source_segment(UI, batch) or ""
check("batch has response/THD/noise/IMD selectors",
      all(f"self.chk_{key}" in batch_text for key in ("response", "thd", "noise", "imd")))
check("default batch includes response, THD and noise, with IMD opt-in",
      all(f"self.chk_{key}.set_active({value})" in batch_text
          for key, value in (("response", "True"), ("thd", "True"),
                             ("noise", "True"), ("imd", "False"))))
check("batch uses one save action for the complete bundle",
      "self.save_button = win.export_measurement_btn" in batch_text
      and '"kind": "dut_batch"' in batch_text
      and "self.win.last = bundle" in batch_text)
check("noise phase offers explicit reconnect handoff",
      "подключи Anti-RIAA" in batch_text and "run_driven_tests" in batch_text)
check("failed subtests are retained instead of aborting all neighbours",
      '"status": "error"' in batch_text and '"tests"' in batch_text)

# Generator page exposes waveforms and both digital/real signal observation.
gen = class_node("GeneratorPage")
gen_text = ast.get_source_segment(UI, gen) or ""
check("generator has sine/triangle/square/sawtooth/white/pink",
      all(f'"{wave}"' in gen_text for wave in
          ("sine", "triangle", "square", "sawtooth", "white", "pink")))
check("oscilloscope can view pre-DAC buffer or physical ADC capture",
      "Буфер генератора · до ЦАП" in gen_text and "Вход АЦП · реальный сигнал" in gen_text)
check("generator preview is cached when parameters do not change",
      "preview_signature == self._last_preview_signature" in gen_text)

# Compact header and automatic mute before measurement pages.
win_init = method_text("Window", "__init__")
page_changed = method_text("Window", "on_page_changed")
check("compact device selector and dedicated top-bar ADC/GEN controls",
      "set_size_request(164, -1)" in win_init and 'ToggleButton(label="АЦП")' in win_init
      and 'ToggleButton(label="ГЕН")' in win_init)
check("generator is disabled on pages other than Live/Generator",
      "page not in (self.live, self.generator_page)" in page_changed
      and "self.generator_btn.set_active(False)" in page_changed)

# Avoid layout churn and needless Cairo redraw requests.
live_tick = method_text("LivePage", "tick")
refresh_health = method_text("Window", "refresh_health")
check("live status no longer embeds ever-growing frame counters",
      "capture_frames" not in live_tick.split("status_text =", 1)[-1]
      and "playback_frames" not in live_tick)
check("meter values update together in a single draw request per tick",
      "redraw=False" in live_tick and live_tick.count("strip.meters.queue_draw()") == 2)
check("clip CSS class changes only when state changes",
      "has_css_class(\"error\")" in live_tick)
check("health banner is updated only when displayed state changes",
      "signature != getattr(self, \"_last_health_display\", None)" in refresh_health)

failed = 0
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    failed += not ok
print(f"UI workflow source checks: {len(checks) - failed}/{len(checks)} PASS")
raise SystemExit(1 if failed else 0)
