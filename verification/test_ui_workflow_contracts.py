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
check("independent DAC/ADC selectors and dedicated top-bar ADC/GEN controls",
      "self.dac_dev = Gtk.DropDown" in win_init and "self.adc_dev = Gtk.DropDown" in win_init
      and 'ToggleButton(label="АЦП")' in win_init and 'ToggleButton(label="ГЕН")' in win_init
      and "header.pack_start(self.dac_dev)" in win_init
      and "header.pack_start(self.adc_dev)" in win_init)
check("generator is disabled on pages other than Live/Generator",
      "page not in (self.live, self.generator_page)" in page_changed
      and "self.generator_btn.set_active(False)" in page_changed)


# Separate endpoints must be wired all the way to ALSA commands and calibration provenance.
backend_source = (ROOT / "valvebench" / "backend.py").read_text(encoding="utf-8")
make_backend = method_text("Window", "make_backend")
context_method = method_text("Window", "_measurement_context")
run_job = method_text("Window", "run_job")
check("backend routes aplay and arecord to separate hw endpoints",
      "self.hw_out" in backend_source and "self.hw_in" in backend_source
      and "endpoint = self.hw_in if direction == \"c\" else self.hw_out" in backend_source)
check("measurement rate choices use the intersection of both PCM directions",
      "common_pcm_rates(dac_hw, adc_hw, RATES)" in method_text("Window", "_rates_for_pair"))
check("backend construction passes selected DAC and ADC separately",
      "AlsaCli(hw_out=dac_hw, hw_in=adc_hw" in make_backend)
check("saved measurement passport records both endpoint identities",
      '"dac_device": dac_hw' in context_method and '"adc_device": adc_hw' in context_method
      and '"device_pair_key": pair_key' in context_method)
check("split-clock precision jobs require a clock preflight",
      "if clock_sensitive and dac_hw != adc_hw" in run_job
      and "m.clock_check(rig, level_dbfs=-72.0)" in run_job
      and 'not clock.get("ok", False)' in run_job)

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

# Header geometry is invariant across status changes and active/inactive states.
check("header status has one fixed width and truncates long text",
      "self.status.set_width_chars(13)" in win_init
      and "self.status.set_max_width_chars(13)" in win_init
      and "self.status.set_ellipsize(3)" in win_init)
sync_gen = method_text("LivePage", "sync_generator_button")
check("generator button keeps constant label/style while toggling",
      "set_label" not in sync_gen and "add_css_class" not in sync_gen
      and "remove_css_class" not in sync_gen)
start_live = method_text("Window", "start_live")
stop_live = method_text("Window", "stop_live")
check("ADC button keeps constant label/style while toggling",
      "live_btn.set_label" not in start_live + stop_live
      and "live_btn.add_css_class" not in start_live + stop_live
      and "live_btn.remove_css_class" not in start_live + stop_live)
widgets_source = (ROOT / "valvebench" / "widgets.py").read_text(encoding="utf-8")
check("persistent health label is single-line, bounded and tooltip-backed",
      "self.health.set_single_line_mode(True)" in widgets_source
      and "self.health.set_ellipsize(3)" in widgets_source
      and "self.health.set_tooltip_text(text)" in widgets_source)

failed = 0
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    failed += not ok
print(f"UI workflow source checks: {len(checks) - failed}/{len(checks)} PASS")
raise SystemExit(1 if failed else 0)
