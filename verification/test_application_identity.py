#!/usr/bin/env python3
"""Pure-Python tests for the installed/development GApplication identity."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("valvebench_identity", ROOT / "valvebench" / "identity.py")
identity = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(identity)

checks: list[tuple[str, bool, str]] = []

def check(name: str, condition: bool, detail: str = "") -> None:
    checks.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f": {detail}" if detail else ""))

with tempfile.TemporaryDirectory(prefix="vb-identity-") as tmp:
    base = Path(tmp)
    a = base / "checkout-a"
    b = base / "checkout-b"
    installed = base / "installed"
    for tree in (a, b, installed):
        (tree / "valvebench").mkdir(parents=True)
    pa = a / "valvebench" / "ui.py"
    pb = b / "valvebench" / "ui.py"
    pi = installed / "valvebench" / "ui.py"
    pa.touch(); pb.touch(); pi.touch()
    (a / "valvebench-build-profile").write_text("development\n")
    (b / "valvebench-build-profile").write_text("development\n")

    ida1 = identity.application_id(pa, {})
    ida2 = identity.application_id(pa, {})
    idb = identity.application_id(pb, {})
    check("development profile from explicit marker", identity.build_profile(pa, {}) == "development")
    check("same development tree has deterministic ID", ida1 == ida2, ida1)
    check("different development trees have different IDs", ida1 != idb, f"A={ida1}; B={idb}")
    check("development IDs use valid D-Bus name syntax", identity._valid_application_id(ida1), ida1)
    check("missing marker defaults safely to installed", identity.build_profile(pi, {}) == "installed")
    check("installed profile keeps stable legacy ID", identity.application_id(pi, {}) == "local.valvebench")
    check("explicit installed env overrides development marker", identity.application_id(pa, {"VALVEBENCH_BUILD_PROFILE": "installed"}) == "local.valvebench")
    check("explicit development env overrides installed default", identity.build_profile(pi, {"VALVEBENCH_BUILD_PROFILE": "development"}) == "development")
    check("full application-id override wins", identity.application_id(pa, {"VALVEBENCH_APPLICATION_ID": "local.valvebench.test.p123"}) == "local.valvebench.test.p123")
    try:
        identity.application_id(pa, {"VALVEBENCH_APPLICATION_ID": "not a valid id"})
        override_rejected = False
    except ValueError:
        override_rejected = True
    check("invalid application-id override is rejected", override_rejected)
    try:
        identity.build_profile(pa, {"VALVEBENCH_BUILD_PROFILE": "typo"})
        profile_rejected = False
    except ValueError:
        profile_rejected = True
    check("unknown build profile is rejected", profile_rejected)
    # The symlink is an alias of the same checkout, not a new checkout identity.
    alias = base / "checkout-a-alias"
    alias.symlink_to(a, target_is_directory=True)
    alias_py = alias / "valvebench" / "ui.py"
    check("symlink alias resolves to canonical checkout ID", identity.application_id(alias_py, {}) == ida1)
    # No verification/ directory is involved in profile selection.
    (a / "verification").mkdir()
    (a / "valvebench-build-profile").write_text("installed\n")
    check("verification directory does not override explicit installed marker", identity.application_id(pa, {}) == "local.valvebench")
    (a / "valvebench-build-profile").write_text("invalid-profile\n")
    try:
        identity.build_profile(pa, {})
        marker_rejected = False
    except ValueError:
        marker_rejected = True
    check("invalid profile marker is rejected", marker_rejected)

passed = sum(ok for _, ok, _ in checks)
print(f"\nIDENTITY TESTS: {passed}/{len(checks)} PASS")
raise SystemExit(0 if passed == len(checks) else 1)
