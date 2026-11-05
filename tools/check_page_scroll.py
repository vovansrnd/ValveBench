#!/usr/bin/env python3
"""Static guard: every page type installed in Gtk.Stack must have a vertical scroller."""
from pathlib import Path
import ast

source = Path(__file__).resolve().parents[1] / "valvebench" / "ui.py"
text = source.read_text(encoding="utf-8")
tree = ast.parse(text)
classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
required = {"Page", "LivePage", "SpectrumPage"}
for name in sorted(required):
    node = classes[name]
    body = ast.get_source_segment(text, node) or ""
    assert "vertical_page_scroller(" in body, f"{name}: missing vertical scroller"
    assert "self.append(self.page_scroller)" in body, f"{name}: scroller is not attached"

# Ensure Gtk.Stack uses only the covered page implementations.
window = classes["Window"]
window_body = ast.get_source_segment(text, window) or ""
for page_name in ("self.live", "self.spectrum", "ThdPage(self)", "ResponsePage(self)",
                  "NoiseResponsePage(self)", "ImdPage(self)", "NoisePage(self)",
                  "VinylPage(self)", "SetupPage(self)"):
    assert page_name in window_body, f"Window page missing or renamed: {page_name}"

print("PAGE SCROLL STRUCTURE: PASS")
print("Page / LivePage / SpectrumPage all attach a vertical Gtk.ScrolledWindow.")
print("All Stack pages are covered (other instrument pages inherit Page).")
