#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
if [[ -n "${VALVEBENCH_PYTHON:-}" ]]; then
  PYTHON_BIN="$VALVEBENCH_PYTHON"
elif [[ -x /usr/bin/python3 ]] && /usr/bin/python3 -c 'import numpy, scipy, gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' >/dev/null 2>&1; then
  # Apt-managed Python exposes the GTK bindings installed by install-deps.sh.
  PYTHON_BIN=/usr/bin/python3
else
  # Keep CLI/self-tests usable in an existing scientific-Python environment.
  PYTHON_BIN="$(command -v python3)"
fi
exec "$PYTHON_BIN" -m valvebench "$@"
