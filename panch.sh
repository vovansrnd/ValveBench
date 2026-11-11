set -euo pipefail

CUR="$PWD"
test -f "$CUR/valvebench/dsp.py" || {
    echo "Запусти команду из корня текущей dev4-копии"
    exit 1
}

BASE_TAR="$(find /opt/MyApps/Adudio -type f \
    -name 'ValveBench-0.1.21-dev1.tar.gz' -print -quit)"

test -n "$BASE_TAR" || {
    echo "Не найден архив dev1"
    exit 1
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$BASE_TAR" -C "$TMP"
BASE="$TMP/ValveBench-0.1.21-dev1"

mkdir -p "$CUR/_handoff"

python3 - "$BASE" "$CUR" "$CUR/_handoff/dev1-to-current-source.patch" <<'PY'
from pathlib import Path
from difflib import unified_diff
import sys

base, current, output = map(Path, sys.argv[1:])

roots = ("valvebench", "verification", "tests")
suffixes = {".py", ".sh"}
excluded = {"__pycache__", "results", "data"}

files = set()

for root in roots:
    for tree in (base / root, current / root):
        if not tree.is_dir():
            continue
        for p in tree.rglob("*"):
            if not p.is_file() or p.suffix not in suffixes:
                continue
            rel = p.relative_to(base if base in p.parents else current)
            if excluded.intersection(rel.parts):
                continue
            files.add(rel)

root_files = {
    "run.sh", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements-local.txt",
}

for p in current.glob("requirements*.txt"):
    root_files.add(p.name)

files.update(Path(x) for x in root_files)

parts = []

for rel in sorted(files, key=str):
    old = base / rel
    new = current / rel

    if old.is_file():
        a = old.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        a = []

    if new.is_file():
        b = new.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        b = []

    if a == b:
        continue

    parts.extend(unified_diff(
        a, b,
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    ))

output.write_text("".join(parts), encoding="utf-8")
print(f"Patch: {output}")
print(f"Изменённых строк: {len(parts)}")
print(f"Размер: {output.stat().st_size} bytes")
PY

sha256sum "$CUR/_handoff/dev1-to-current-source.patch"
