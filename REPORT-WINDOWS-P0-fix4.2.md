# ValveBench fix4.2 — явный профиль сборки и безопасные тесты одиночного окна

Основа: `ValveBench-0.1.20-dev3-reviewfix4.1-singlewindow.tar.gz`.
Пользовательская установленная копия не изменялась.

## Исправления

1. Удалена эвристика `verification/` из определения `application_id`.
   Новый `valvebench/identity.py` использует явный профиль в порядке приоритета:
   - `VALVEBENCH_APPLICATION_ID` — полный override;
   - `VALVEBENCH_BUILD_PROFILE=development|installed`;
   - файл `valvebench-build-profile` в корне дерева;
   - если явной настройки нет — безопасный профиль `installed`.
2. Development ID строится от канонического пути дерева. Две разные рабочие
   копии изолированы, повторный запуск той же копии сохраняет ID, symlink на
   то же дерево не создаёт новый ID. Installed ID остаётся `local.valvebench`.
3. Неизвестный профиль и некорректный `VALVEBENCH_APPLICATION_ID` вызывают
   явную ошибку вместо тихого fallback.
4. В архиве dev-дерева добавлен `valvebench-build-profile` со значением
   `development`. Для маркировки установленного дерева предусмотрен
   `tools/set-build-profile.sh installed`. Существующая установленная копия
   без маркера автоматически получает стабильный installed ID.
5. Исправлен тестовый ID в `test_app_single_window.py`: PID теперь
   интерполируется, а не остаётся буквальным `{os.getpid()}`.
6. `test_single_window_e2e.sh` переписан без глобального `pkill`: тест завершает
   только процессы, которые сам запустил, использует собственный D-Bus,
   уникальный Xvfb display, отдельный `XDG_RUNTIME_DIR`, `GDK_BACKEND=x11` и
   сбрасывает `WAYLAND_DISPLAY`.
7. `test_dev_ids_e2e.sh` использует уникальный временный каталог для копий,
   логов и runtime dir; проверяет zombie-aware состояние PID и завершает только
   собственные процессы.

## Проверки, выполненные в доступной среде

| Проверка | Результат |
|---|---:|
| Identity unit tests | **14/14 PASS** |
| `./run.sh selftest` | **96/96 PASS** |
| `python3 -m compileall -q valvebench verification` | PASS |
| `bash -n` для обоих E2E runner | PASS |
| `./run.sh --version` | PASS (`0.1.20-dev3`) |
| Поиск старой эвристики `verification/` в identity code | не найдена |

## Что не заявляется

В контейнере отсутствуют PyGObject/GTK4 и SciPy для системного Python.
Поэтому in-process GTK test и оба графических D-Bus/Xvfb E2E здесь не запускались.
Никаких аппаратных измерений или проверки реального Wayland GUI этот отчёт не
заявляет. Их нужно выполнить на машине пользователя в отдельном контролируемом
сеансе после установки необходимых зависимостей.

## Количество проверок fix4.1

В `test_dev_ids_e2e.sh` подсчитано **18** вызовов assertion helpers `pf`/`pfz`.
Число 19 в исходных материалах не подтверждается кодом runner; в этом отчёте
оно не используется. Это подсчёт проверок сценария, а не повторный запуск E2E.
