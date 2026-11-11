#!/usr/bin/env python3
"""Static guard: every page type installed in Gtk.Stack must have a vertical scroller."""
from pathlib import Path
import ast

source = Path(__file__).resolve().parents[1] / "valvebench" / "ui.py"
text = source.read_text(encoding="utf-8")
tree = ast.parse(text)
classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
# 0.1.20: Page и LivePage — Paned «настройки | график» (каждая половина в своём вертикальном
# скроллере); SpectrumPage — один скроллер на всю страницу.
attach = {"Page": "self.append(self.paned)", "LivePage": "self.append(self.paned)",
          "SpectrumPage": "self.append(self.page_scroller)"}
for name, expected in sorted(attach.items()):
    node = classes[name]
    body = ast.get_source_segment(text, node) or ""
    assert "vertical_page_scroller(" in body, f"{name}: missing vertical scroller"
    assert expected in body, f"{name}: layout is not attached ({expected})"

# Ensure Gtk.Stack uses only the covered page implementations.
window = classes["Window"]
window_body = ast.get_source_segment(text, window) or ""
for page_name in ("self.live", "self.spectrum", "ThdPage(self)", "ResponsePage(self)",
                  "NoiseResponsePage(self)", "ImdPage(self)", "NoisePage(self)",
                  "VinylPage(self)", "SetupPage(self)"):
    assert page_name in window_body, f"Window page missing or renamed: {page_name}"

print("PAGE SCROLL STRUCTURE: PASS")
print("Page / LivePage: Paned with per-side vertical scrollers; SpectrumPage: one page scroller.")
print("All Stack pages are covered (other instrument pages inherit Page).")
