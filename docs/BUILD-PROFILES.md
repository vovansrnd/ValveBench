# Профили запуска ValveBench

`GApplication` использует application ID как ключ единственного экземпляра в
пользовательской D-Bus-сессии. Поэтому у разных рабочих копий должны быть разные
ID, а установленная копия должна сохранять один стабильный ID.

## Профили

- `development`: ID `local.valvebench.dev.p<12 hex>` вычисляется из canonical
  realpath корня checkout. Два checkout из разных каталогов независимы.
- `installed`: стабильный ID `local.valvebench`.

Профиль выбирается в следующем порядке:

1. `VALVEBENCH_APPLICATION_ID` — полный override (должен быть корректным
   D-Bus application ID).
2. `VALVEBENCH_BUILD_PROFILE=development|installed`.
3. Файл `valvebench-build-profile` в корне дерева.
4. Если ничего не задано — безопасный default `installed`.

В dev-архиве маркер уже содержит `development`. При переносе/установке дерева
в постоянный каталог установленной программы обязательно переключить профиль:

```bash
./tools/set-build-profile.sh installed
```

Для возвращения копии в dev-режим:

```bash
./tools/set-build-profile.sh development
```

У существующей установленной копии без маркера ничего менять не нужно: она
получит `local.valvebench` по default. Папка `verification/` на выбор профиля
больше не влияет.
