#!/usr/bin/env python3
"""Static UI contract for the saved-DUT comparison page (no GTK runtime required)."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE = ROOT / "valvebench" / "ui.py"


class DutComparisonUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.page = next(
            node for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DutComparisonPage"
        )
        cls.methods = {
            node.name: node for node in cls.page.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_page_is_registered_in_window_navigation(self):
        self.assertIn('self.compare_page = DutComparisonPage(self)', self.source)
        self.assertIn('("compare", "Сравнение DUT", self.compare_page)', self.source)

    def test_page_lists_and_compares_saved_sessions(self):
        for method in ("refresh_sessions", "compare_selected", "_comparison_finished"):
            self.assertIn(method, self.methods)
        self.assertIn("dutcompare.discover_sessions", self.source)
        self.assertIn("dutcompare.compare_sessions", self.source)
        self.assertIn("dutcompare.save_comparison", self.source)

    def test_file_comparison_uses_worker_threads_not_audio_run_job(self):
        worker_calls = []
        run_job_calls = []
        for node in ast.walk(self.page):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "Thread":
                worker_calls.append(node)
            if isinstance(func, ast.Attribute) and func.attr == "run_job":
                run_job_calls.append(node)
        self.assertGreaterEqual(len(worker_calls), 2)
        self.assertEqual(run_job_calls, [])

    def test_user_can_select_multiple_sessions(self):
        self.assertIn("Gtk.CheckButton", self.source)
        self.assertIn("len(paths) < 2", self.source)
        self.assertIn("СРАВНИТЬ ВЫБРАННЫЕ СЕССИИ", self.source)

    def test_full_response_plot_keeps_left_and_right_distinct(self):
        self.assertIn("self.response_plot = Plot(xlog=True", self.source)
        self.assertIn('"label": f"{label} L"', self.source)
        self.assertIn('"label": f"{label} R"', self.source)
        self.assertIn('"dash": True', self.source)
        self.assertIn('curve.get("L_db", [])', self.source)
        self.assertIn('curve.get("R_db", [])', self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
