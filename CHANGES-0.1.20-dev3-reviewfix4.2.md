# fix4.2 — явный профиль приложения и безопасные оконные тесты

- Вынесена логика `application_id` в pure-Python `valvebench/identity.py`.
- Убрана зависимость от наличия `verification/`; профиль задаётся env или
  `valvebench-build-profile`, отсутствие маркера означает installed.
- Development ID детерминирован по canonical realpath; installed ID сохранён.
- Добавлена валидация профиля и D-Bus application ID.
- Исправлена интерполяция PID в in-process test.
- E2E single-window больше не использует `pkill`; тесты изолируют runtime,
  D-Bus и X display.
- Добавлен профильный тест без GTK-зависимости.
