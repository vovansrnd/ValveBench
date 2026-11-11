"""Stable, explicit GApplication identity for installed and development trees.

Profile selection order:
1. VALVEBENCH_APPLICATION_ID: explicit full D-Bus application-id override.
2. VALVEBENCH_BUILD_PROFILE: explicit ``development`` or ``installed``.
3. ``valvebench-build-profile`` file at the source-tree root.
4. Safe default: ``installed`` (never infer development from incidental files).

Development IDs include the canonical source-tree path, so two checkouts do not
forward activation to one another. Symlinks to the same checkout intentionally
resolve to the same ID.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_PROFILE_FILE = "valvebench-build-profile"
_VALID_PROFILES = {"development", "installed"}
_APP_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$")


def source_root(package_file: str | os.PathLike[str] = __file__) -> Path:
    return Path(package_file).resolve().parent.parent


def build_profile(
    package_file: str | os.PathLike[str] = __file__,
    environ: dict[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("VALVEBENCH_BUILD_PROFILE")
    if explicit is not None:
        profile = explicit.strip().lower()
    else:
        marker = source_root(package_file) / _PROFILE_FILE
        try:
            profile = marker.read_text(encoding="ascii").strip().lower()
        except FileNotFoundError:
            profile = "installed"
    if profile not in _VALID_PROFILES:
        raise ValueError(
            f"Invalid ValveBench build profile {profile!r}; "
            "expected 'development' or 'installed'"
        )
    return profile


def application_id(
    package_file: str | os.PathLike[str] = __file__,
    environ: dict[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    override = env.get("VALVEBENCH_APPLICATION_ID")
    if override is not None:
        app_id = override.strip()
        if not _valid_application_id(app_id):
            raise ValueError(f"Invalid VALVEBENCH_APPLICATION_ID: {app_id!r}")
        return app_id

    if build_profile(package_file, env) == "installed":
        return "local.valvebench"

    root = source_root(package_file)
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    return f"local.valvebench.dev.p{digest}"


def _valid_application_id(value: str) -> bool:
    # GApplication requires a valid, non-unique D-Bus well-known name.
    return bool(value) and len(value) <= 255 and _APP_ID_RE.fullmatch(value) is not None
