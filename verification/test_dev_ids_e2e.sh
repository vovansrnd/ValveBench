#!/usr/bin/env bash
# P0.1 «изоляция копий»: два рабочих дерева A и B в ОДНОЙ D-Bus-сессии.
#
# Последовательность координатора:
#   1. Запустить A              → окно A
#   2. Запустить A снова        → всё ещё одно окно A (remote-активация)
#   3. Запустить B              → запускается ИМЕННО B (свой процесс и окно,
#                                 а НЕ активация A!) — ключевая проверка
#   4. Запустить B снова        → всё ещё одно окно B
#   5. Запустить A снова        → активируется существующее окно A
#   6. Закрыть A, запустить A   → одно новое окно A
#
# Атрибуция окон процессам: _NET_WM_PID (ставит GTK). Идентификаторы
# приложений: имена на шине D-Bus (local.valvebench.dev.p<hash>).
# Тест ПРОВАЛИВАЕТСЯ, если запуск B просто активировал A.
#
# Использование: test_dev_ids_e2e.sh [ИСХОДНОЕ_ДЕРЕВО]
if [ "${1:-}" != "__inner" ]; then
    TREE="${1:-$(dirname "$0")/..}"
    TREE="$(cd "$TREE" && pwd)"
    TMP_BASE="$(mktemp -d -t valvebench-dev-ids.XXXXXX)"
    export VB_TEST_TMP="$TMP_BASE"
    export VERDICT_FILE="$TMP_BASE/verdict.txt"
    set +e
    timeout -k 5 360 dbus-run-session -- bash "$0" __inner "$TREE"
    rc=$?
    set -e
    if grep -q '^FAIL ' "$VERDICT_FILE" 2>/dev/null; then echo "FAIL: see $TMP_BASE"; exit 1; fi
    if grep -q '^PASS ' "$VERDICT_FILE" 2>/dev/null; then echo "PASS: evidence in $TMP_BASE"; exit 0; fi
    echo "нет вердикта — тест не дошёл до конца (exit=$rc); logs: $TMP_BASE"; exit 1
fi
set -u
SRC_TREE="${2:-.}"
SRC_TREE="$(cd "$SRC_TREE" && pwd)"

TMP_BASE="${VB_TEST_TMP:?temporary directory missing}"
A="$TMP_BASE/tree-A"
B="$TMP_BASE/tree-B"
VERDICT="${VERDICT_FILE:?verdict file missing}"
: > "$VERDICT"

say()  { echo "$@" | tee -a "$VERDICT" >/dev/null; }
pf()   { # pf имя факт ожидание
    if [ "$2" -eq "$3" ]; then say "PASS $1: $2 (ожидалось $3)"; else say "FAIL $1: $2 (ожидалось $3)"; fi
}
pfz()  { if [ "$2" = "$3" ]; then say "PASS $1: $2"; else say "FAIL $1: $2 (ожидалось $3)"; fi }

dev_id() {  # dev-id дерева (как в ui._application_id)
    /usr/bin/python3 - "$1" << 'PYEOF'
import hashlib, os, sys
print("local.valvebench.dev.p" + hashlib.sha256(
    os.path.realpath(sys.argv[1]).encode()).hexdigest()[:12])
PYEOF
}

windows_of_pid() {  # сколько X-окон ValveBench принадлежит процессу $1
    local pid="$1" n=0 id
    for id in $(xwininfo -root -tree 2>/dev/null | grep ValveBench | grep -oP '^\s+\K0x[0-9a-f]+'); do
        [ "$(xprop -id "$id" _NET_WM_PID 2>/dev/null | grep -oP '\d+\s*$' | tr -d ' ')" = "$pid" ] && n=$((n+1))
    done
    echo "$n"
}

total_windows() { xwininfo -root -tree 2>/dev/null | grep -c "ValveBench"; }
alive() {
    local state
    kill -0 "$1" 2>/dev/null || { echo нет; return; }
    state="$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')"
    [[ -n "$state" && "$state" != Z* && "$state" != X* ]] && echo да || echo нет
}

bus_names() { busctl --user list --no-legend 2>/dev/null | awk '{print $1}' | grep -c "valvebench"; }
has_name() { busctl --user list --no-legend 2>/dev/null | awk '{print $1}' | grep -qx "$1" && echo да || echo нет; }

# --- подготовка: два независимых дерева с verification/ (обе — dev-копии)
mkdir -p "$TMP_BASE"
cp -a "$SRC_TREE" "$A"
cp -a "$SRC_TREE" "$B"
export XDG_RUNTIME_DIR="$(mktemp -d "$TMP_BASE/runtime.XXXXXX")"
chmod 700 "$XDG_RUNTIME_DIR"

XVFB_PID=""
LAUNCHED=""   # PID процессов, запущенных самим тестом
cleanup() {
    [ -n "${XVFB_PID:-}" ] && kill "$XVFB_PID" 2>/dev/null
    for p in $LAUNCHED; do kill "$p" 2>/dev/null || true; done
    for p in $LAUNCHED; do wait "$p" 2>/dev/null || true; done
    [ -n "${XVFB_PID:-}" ] && wait "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT

# уникальный дисплей на прогон: прошлые Xvfb не должны мешать
for D in $(seq 90 180); do
    if ! xdpyinfo -display ":$D" >/dev/null 2>&1; then DISP=$D; break; fi
done
DISP=${DISP:-131}
Xvfb ":$DISP" -screen 0 1280x800x24 &
XVFB_PID=$!
for _ in $(seq 1 20); do xdpyinfo -display ":$DISP" >/dev/null 2>&1 && break; sleep 0.5; done
xdpyinfo -display ":$DISP" >/dev/null 2>&1 || { echo "FAIL: Xvfb :$DISP не поднялся"; exit 1; }
export DISPLAY=":$DISP"
export GDK_BACKEND=x11   # принудительно используем Xvfb
unset WAYLAND_DISPLAY
unset VALVEBENCH_APPLICATION_ID VALVEBENCH_BUILD_PROFILE

ID_A=$(dev_id "$A")
ID_B=$(dev_id "$B")
say "дерево A: $A  id=$ID_A"
say "дерево B: $B  id=$ID_B"
pfz "ids различаются для разных деревьев" "$([ "$ID_A" != "$ID_B" ] && echo да || echo нет)" "да"

wait_win() {  # $1 pid-владелец, $2 сколько секунд ждать его окно
    for _ in $(seq 1 "${2:-30}"); do
        [ "$(windows_of_pid "$1")" -ge 1 ] && return 0
        sleep 1
    done
    return 1
}

# --- 1. Запустить A
cd "$A"
./run.sh > "$TMP_BASE/dev-ids-A1.log" 2>&1 &
PA=$!; LAUNCHED="$LAUNCHED $PA"
wait_win "$PA" 40 || { say "FAIL: окно A не появилось"; tail -5 "$TMP_BASE/dev-ids-A1.log"; exit 1; }
sleep 4
say "шаг 1: A запущен: PID=$PA, жив=$(alive $PA), окон у A=$(windows_of_pid $PA), всего=$(total_windows)"
pf "шаг 1: запуск A даёт одно окно A" "$(windows_of_pid $PA)" 1
pf "шаг 1: процесс A жив" "$([ "$(alive $PA)" = да ] && echo 1 || echo 0)" 1
pfz "шаг 1: id A владеет шиной" "$(has_name "$ID_A")" "да"

# --- 2. Запустить A снова
cd "$A"
./run.sh > "$TMP_BASE/dev-ids-A2.log" 2>&1 &
PA2=$!; LAUNCHED="$LAUNCHED $PA2"
sleep 12
say "шаг 2: повторный A: второй PID=$PA2 жив=$(alive $PA2), окон у A=$(windows_of_pid $PA), всего=$(total_windows)"
pfz "шаг 2: второй процесс A завершился (remote-активация своей копии)" "$(alive $PA2)" "нет"
pf "шаг 2: окон по-прежнему одно (у A)" "$(windows_of_pid $PA)" 1

# --- 3. Запустить B — ключевой шаг: B НЕ должен активировать A
cd "$B"
./run.sh > "$TMP_BASE/dev-ids-B1.log" 2>&1 &
PB=$!; LAUNCHED="$LAUNCHED $PB"
sleep 15
say "шаг 3: B запущен: PID=$PB, жив=$(alive $PB), окон у B=$(windows_of_pid $PB), всего=$(total_windows)"
pf "шаг 3: процесс B ЖИВ (не remote-активация A)" "$([ "$(alive $PB)" = да ] && echo 1 || echo 0)" 1
pf "шаг 3: у B своё окно" "$(windows_of_pid $PB)" 1
pfz "шаг 3: id B владеет шиной" "$(has_name "$ID_B")" "да"
pf "шаг 3: всего окон ровно два (A и B)" "$(total_windows)" 2

# --- 4. Запустить B снова
cd "$B"
./run.sh > "$TMP_BASE/dev-ids-B2.log" 2>&1 &
PB2=$!; LAUNCHED="$LAUNCHED $PB2"
sleep 12
say "шаг 4: повторный B: второй PID=$PB2 жив=$(alive $PB2), всего=$(total_windows)"
pfz "шаг 4: второй процесс B завершился (remote-активация своей копии)" "$(alive $PB2)" "нет"
pf "шаг 4: всего окон по-прежнему два" "$(total_windows)" 2

# --- 5. Запустить A снова (повторная активация A)
cd "$A"
./run.sh > "$TMP_BASE/dev-ids-A3.log" 2>&1 &
PA3=$!; LAUNCHED="$LAUNCHED $PA3"
sleep 12
say "шаг 5: третий запуск A: PID=$PA3 жив=$(alive $PA3), окон у A=$(windows_of_pid $PA), всего=$(total_windows)"
pfz "шаг 5: процесс завершился (активировано существующее окно A)" "$(alive $PA3)" "нет"
pf "шаг 5: окно A то же (владелец прежний PID=$PA)" "$(windows_of_pid $PA)" 1
pf "шаг 5: всего окон два" "$(total_windows)" 2

# --- 6. Закрыть A, запустить A заново
kill "$PA" 2>/dev/null
sleep 5
say "шаг 6: A закрыт: всего окон=$(total_windows)"
pf "шаг 6: после закрытия A осталось окно B" "$(total_windows)" 1
cd "$A"
./run.sh > "$TMP_BASE/dev-ids-A4.log" 2>&1 &
PA4=$!; LAUNCHED="$LAUNCHED $PA4"
wait_win "$PA4" 40 || { say "FAIL: новое окно A не появилось"; exit 1; }
sleep 4
say "шаг 6: A запущен заново: новый PID=$PA4 (старый был $PA), окон у нового A=$(windows_of_pid $PA4), всего=$(total_windows)"
pf "шаг 6: новое окно принадлежит НОВОМУ процессу" "$(windows_of_pid $PA4)" 1
pf "шаг 6: всего окон два (новое A + B)" "$(total_windows)" 2

say "=== application_id / PID / realpath ==="
say "A: $ID_A  PID(последний)=${PA4}  realpath=$(readlink -f "$A")"
say "B: $ID_B  PID=${PB}  realpath=$(readlink -f "$B")"

grep -q FAIL "$VERDICT" && exit 1 || exit 0
