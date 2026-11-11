#!/usr/bin/env bash
# Контрольная приёмка P0 «два окна»: end-to-end на изолированном стенде.
#
# Свой D-Bus (dbus-run-session) + свой Xvfb: запуск ValveBench здесь не
# касается ни сессии пользователя, ни работающих экземпляров. Окна считаются
# по xwininfo -root -tree (работает без оконного менеджера: видны все
# созданные X-окна с заголовком ValveBench).
#
# Сценарии (таблица аудитора):
#   1) первый запуск            → 1 окно
#   2) повторный ./run.sh       → окно НЕ добавляется
#   3) третий запуск            → окно НЕ добавляется
#   4) закрытие и новый запуск  → 1 новое окно (после ухода первого)
#
# Использование: test_single_window_e2e.sh [ДЕРЕВО]
if [ "${1:-}" != "__inner" ]; then
    TREE="${1:-$(dirname "$0")/..}"
    TREE="$(cd "$TREE" && pwd)"
    rm -f /tmp/single-window-verdict.txt
    # dbus-run-session может не вернуть управление: порталы сессии живут дольше
    # теста — потому timeout; итог берём из verdict-файла, а не из кода выхода
    timeout -k 5 300 dbus-run-session -- bash "$0" __inner "$TREE"
    grep -q FAIL /tmp/single-window-verdict.txt 2>/dev/null && exit 1
    grep -q PASS /tmp/single-window-verdict.txt 2>/dev/null && exit 0
    echo "нет вердикта — тест не дошёл до конца"; exit 1
fi
TREE="${2:-.}"
cd "$TREE" || exit 1

COUNT=0
count_windows() {
    xwininfo -root -tree 2>/dev/null | grep -ci "ValveBench"
}

wait_windows() {  # $1 = сколько секунд ждать появления ≥1 окна
    for _ in $(seq 1 "${1:-30}"); do
        COUNT=$(count_windows)
        [ "$COUNT" -ge 1 ] && return 0
        sleep 1
    done
    return 1
}

settle_windows() {  # дать GTK дописать все окна после активации
    sleep "${1:-6}"
    COUNT=$(count_windows)
}

run_app() {  # $1 тег лога → эхо PID
    nohup ./run.sh > "/tmp/single-window-$1.log" 2>&1 &
    echo $!
}

pass_fail() { # $1 имя, $2 факт, $3 ожидание
    if [ "$2" -eq "$3" ]; then
        echo "PASS $1: окон = $2 (ожидалось $3)"
    else
        echo "FAIL $1: окон = $2 (ожидалось $3)"
    fi
}

VERDICT=/tmp/single-window-verdict.txt
: > "$VERDICT"

export XDG_RUNTIME_DIR="$(mktemp -d)"
chmod 700 "$XDG_RUNTIME_DIR"

XVFB_PID=""
cleanup() { [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null; pkill -f "python.*valvebench" 2>/dev/null; }
trap cleanup EXIT

Xvfb :97 -screen 0 1280x800x24 &
XVFB_PID=$!
sleep 1.5
export DISPLAY=:97
export GDK_BACKEND=x11

echo "=== Сценарий 1: первый запуск ==="
P1=$(run_app first)
if wait_windows 40; then :; else echo "FAIL: окна не появились за 40 с"; tail -5 /tmp/single-window-first.log; exit 1; fi
settle_windows 5
N1=$COUNT
echo "      первичный процесс: $(pgrep -af "python.*valvebench" | head -1 | cut -c1-90)"

echo "=== Сценарий 2: повторный ./run.sh при открытом приложении ==="
P2=$(run_app second)
sleep 15
settle_windows 2
N2=$COUNT
if kill -0 "$P2" 2>/dev/null; then STATE2="жив (стал новым primary?)"; else STATE2="завершился (remote-активация первичного)"; fi
echo "      второй процесс: $STATE2"

echo "=== Сценарий 3: третий запуск ==="
P3=$(run_app third)
sleep 15
settle_windows 2
N3=$COUNT

echo "=== Сценарий 4: закрыть всё, запустить заново ==="
pkill -f "python.*valvebench" 2>/dev/null
sleep 3
P4=$(run_app fourth)
wait_windows 40
settle_windows 5
N4=$COUNT
pkill -f "python.*valvebench" 2>/dev/null
sleep 1

echo "=== ИТОГИ ==="
pass_fail "1-й запуск: одно окно"           "$N1" 1 | tee -a "$VERDICT"
pass_fail "повторный запуск: по-прежнему 1" "$N2" 1 | tee -a "$VERDICT"
pass_fail "третий запуск: по-прежнему 1"    "$N3" 1 | tee -a "$VERDICT"
pass_fail "после закрытия: 1 новое окно"    "$N4" 1 | tee -a "$VERDICT"
echo "$N1 $N2 $N3 $N4" > /tmp/single-window-counts.txt
exit 0
