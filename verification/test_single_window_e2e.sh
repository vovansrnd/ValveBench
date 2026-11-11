#!/usr/bin/env bash
# Безопасный E2E single-window: собственный D-Bus + Xvfb + runtime dir.
# Не использует pkill и не может завершить пользовательский ValveBench.
set -Eeuo pipefail

if [[ "${1:-}" != "__inner" ]]; then
    TREE="${1:-$(dirname "$0")/..}"
    TREE="$(cd "$TREE" && pwd)"
    TMP_BASE="$(mktemp -d -t valvebench-single-window.XXXXXX)"
    export VB_TEST_TMP="$TMP_BASE"
    set +e
    timeout -k 5 240 dbus-run-session -- bash "$0" __inner "$TREE"
    rc=$?
    set -e
    if grep -q '^FAIL ' "$TMP_BASE/verdict.txt" 2>/dev/null; then
        echo "E2E failed; evidence retained in $TMP_BASE" >&2
        exit 1
    fi
    if grep -q '^PASS ' "$TMP_BASE/verdict.txt" 2>/dev/null; then
        echo "E2E passed; evidence: $TMP_BASE/verdict.txt"
        rm -rf "$TMP_BASE"
        exit 0
    fi
    echo "E2E did not produce a verdict (exit=$rc); evidence retained in $TMP_BASE" >&2
    exit 1
fi

TREE="$(cd "${2:?source tree required}" && pwd)"
TMP_BASE="${VB_TEST_TMP:?temporary directory missing}"
VERDICT="$TMP_BASE/verdict.txt"
: > "$VERDICT"
PIDS=()
XVFB_PID=""
RUNTIME_DIR="$(mktemp -d "$TMP_BASE/runtime.XXXXXX")"
chmod 700 "$RUNTIME_DIR"
export XDG_RUNTIME_DIR="$RUNTIME_DIR"
export GDK_BACKEND=x11
unset WAYLAND_DISPLAY
unset VALVEBENCH_APPLICATION_ID VALVEBENCH_BUILD_PROFILE

cleanup() {
    local p
    for p in "${PIDS[@]:-}"; do
        kill "$p" 2>/dev/null || true
    done
    for p in "${PIDS[@]:-}"; do
        wait "$p" 2>/dev/null || true
    done
    if [[ -n "$XVFB_PID" ]]; then kill "$XVFB_PID" 2>/dev/null || true; wait "$XVFB_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

say() { printf '%s\n' "$*" | tee -a "$VERDICT"; }
count_windows() { xwininfo -root -tree 2>/dev/null | grep -ci 'ValveBench' || true; }
proc_alive() {
    local state
    kill -0 "$1" 2>/dev/null || return 1
    state="$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')"
    [[ -n "$state" && "$state" != Z* && "$state" != X* ]]
}
wait_windows() {
    local i
    for i in $(seq 1 "${1:-40}"); do
        [[ "$(count_windows)" -ge 1 ]] && return 0
        sleep 1
    done
    return 1
}
run_app() {
    local tag="$1"
    (cd "$TREE" && exec ./run.sh) >"$TMP_BASE/$tag.log" 2>&1 &
    local p=$!
    PIDS+=("$p")
    LAST_PID="$p"
}
wait_exit() {
    local pid="$1" i
    for i in $(seq 1 20); do
        proc_alive "$pid" || return 0
        sleep 0.5
    done
    return 1
}

# Find an unused X display rather than colliding with a developer's Xvfb.
for d in $(seq 90 180); do
    if ! xdpyinfo -display ":$d" >/dev/null 2>&1; then DISPLAY=":$d"; break; fi
done
: "${DISPLAY:?could not find a free X display}"
export DISPLAY
Xvfb "$DISPLAY" -screen 0 1280x800x24 >"$TMP_BASE/xvfb.log" 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 30); do xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break; sleep 0.2; done
xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || { say "FAIL Xvfb did not start"; exit 1; }

say "isolated display=$DISPLAY; runtime=$XDG_RUNTIME_DIR; wayland unset"
run_app first; P1="$LAST_PID"
wait_windows 45 || { say "FAIL first launch did not create a window"; tail -20 "$TMP_BASE/first.log"; exit 1; }
sleep 2
N1="$(count_windows)"
say "first launch: windows=$N1 pid=$P1"

run_app second; P2="$LAST_PID"
wait_exit "$P2" || { say "FAIL second process did not exit as remote activation"; tail -20 "$TMP_BASE/second.log"; exit 1; }
sleep 2
N2="$(count_windows)"
say "second launch: windows=$N2 remote-process-exited=yes"

run_app third; P3="$LAST_PID"
wait_exit "$P3" || { say "FAIL third process did not exit as remote activation"; tail -20 "$TMP_BASE/third.log"; exit 1; }
sleep 2
N3="$(count_windows)"
say "third launch: windows=$N3 remote-process-exited=yes"

kill "$P1" 2>/dev/null || true
wait "$P1" 2>/dev/null || true
sleep 1
N_CLOSED="$(count_windows)"
run_app fourth; P4="$LAST_PID"
wait_windows 45 || { say "FAIL new launch after close did not create a window"; tail -20 "$TMP_BASE/fourth.log"; exit 1; }
sleep 2
N4="$(count_windows)"
say "relaunch after close: windows=$N4"

check_count() {
    local name="$1" actual="$2" expected="$3"
    if [[ "$actual" -eq "$expected" ]]; then
        say "PASS $name ($actual)"
    else
        say "FAIL $name (actual=$actual expected=$expected)"
    fi
}
check_count "first launch: one window" "$N1" 1
check_count "second launch: still one window" "$N2" 1
check_count "third launch: still one window" "$N3" 1
check_count "closed primary leaves zero windows" "$N_CLOSED" 0
check_count "relaunch after close: one new window" "$N4" 1
