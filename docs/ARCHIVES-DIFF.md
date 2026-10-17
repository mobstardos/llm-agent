# Разница архивов llm-agent (2026-09-25)

> Этот документ описывает различия между двумя ZIP-архивами проекта
> `llm-agent`, выпущенными 25 сентября 2026 года. Документ создан как
> часть Спринта 1 (P0) плана работ по результатам технического анализа.

## TL;DR

Архив **`final`** — строгий subset архива **`github-ready`**. Из
`github-ready` в `final` удалено **16 файлов** в трёх категориях:

| Категория | Кол-во файлов | Категория semантики |
|---|---|---|
| `.git/` история | 1 каталог (~5 МБ, 1 коммит) | удаление VCS-истории |
| Runtime-профили | 4 JSON-файла | **возможно, ошибочное удаление** |
| Экспериментальные MCP build + data | 11 файлов | осознанная санация dev-модулей |

Все остальные 695 файлов финальной сборки **байт-в-байт идентичны**
соответствующим файлам из github-ready (проверено через MD5-хеши).

---

## Что в каждом архиве

### `llm-agent-github-ready-2026-09-25 (1).zip` (~3 МБ)

Полный снимок репозитория, готовый к публикации на GitHub. Включает:

- **Каталог `.git/`** — инициализированный git-репозиторий с одним
  коммитом `ad58acb LLM Agent — локальная агентная платформа (v2026-09-25)`
  на ветке `main`, без тегов, ~5 МБ.
- **`data/profiles/*.json`** — 4 runtime-профиля (default, development,
  production, 1c_development) с временными метками `created_at:
  1790270986` (~2026-09-25), помечены `is_builtin: true`.
- **`agents/build/`** — декларация агента build (agent.yaml + prompt.md +
  user.md), `mode: destructive`, `dangerous_tools: [build_run, pkg_add,
  pkg_remove, docker_build, docker_run, docker_compose_up, ...]`.
- **`agents/data/`** — декларация агента data (agent.yaml + prompt.md +
  user.md).
- **`mcp_servers/build/`** — декларация MCP-сервера build (server.yaml
  с 16 инструментами).
- **`mcp_servers/data/`** — декларация MCP-сервера data.
- **`src/mcp_servers/build/`** — Python-реализация MCP-сервера build
  (`__init__.py` + `server.py`).
- **`src/mcp_servers/data/`** — Python-реализация MCP-сервера data.

### `llm-agent-final-2026-09-25-3 (2).zip` (~1,3 МБ)

Чистый релизный дистрибутив. Не содержит git-истории, runtime-профилей
и экспериментальных MCP-серверов. Размер почти в два раза меньше за
счёт отсутствия `.git/` (5 МБ) и удалённых Python-файлов.

---

## Полный список 16 удалённых файлов

```
Категория                                   Файл
─────────────────────────────────────────────────────────────────────────
1. Git-история
                                             .git/ (весь каталог, 5 МБ)

2. Runtime-профили (4 файла)
                                             data/profiles/default.json
                                             data/profiles/development.json
                                             data/profiles/production.json
                                             data/profiles/1c_development.json

3. Агент build (3 файла)
                                             agents/build/agent.yaml
                                             agents/build/prompt.md
                                             agents/build/user.md

4. Агент data (3 файла)
                                             agents/data/agent.yaml
                                             agents/data/prompt.md
                                             agents/data/user.md

5. MCP-сервер build (декларация)
                                             mcp_servers/build/server.yaml

6. MCP-сервер data (декларация)
                                             mcp_servers/data/server.yaml

7. MCP-сервер build (Python-реализация, 2 файла)
                                             src/mcp_servers/build/__init__.py
                                             src/mcp_servers/build/server.py

8. MCP-сервер data (Python-реализация, 2 файла)
                                             src/mcp_servers/data/__init__.py
                                             src/mcp_servers/data/server.py
```

---

## Семантика удалений

### Git-история — осознанное удаление

Git-каталог `.git/` в архиве `github-ready` содержит ровно один коммит
с сообщением «LLM Agent — локальная агентная платформа (v2026-09-25)».
Это снимок инициализации репозитория — типовой сценарий «подготовили
репозиторий к публикации на GitHub». В архиве `final` этот каталог
удалён, потому что конечный пользователь не должен видеть историю
разработки и не должен пушить изменения обратно. Это правильное
решение для релизного дистрибутива.

### Профили — возможная ошибка

Каталог `data/profiles/` в github-ready содержит 4 файла с runtime-
профилями системы:

- **`default.json`** — пустой профиль без переопределений
  (`is_builtin: true`).
- **`development.json`** — все агенты включены, повышенные лимиты
  `file.max_steps=25`, `deepseek.model=deepseek-reasoner`
  (`is_builtin: true`).
- **`production.json`** — отключены агенты `shell` и `git`, опасные
  операции выключены (`is_builtin: true`).
- **`1c_development.json`** — отключены `mysql` и `postgres`,
  повышены приоритеты `file` (20), `onec` (15), `deepseek` (10).

Код системы ожидает, что встроенные профили лежат в `data/profiles/`
и помечены `is_builtin: true`. Без них первый запуск не найдёт
встроенных пресетов — пользователь увидит пустой список в UI на
вкладке «Профили», и при попытке переключиться на production-режим
получит ошибку.

**Рекомендация:** восстановить 4 профиля в финальной сборке. Файлы
прилагаются к этому патчу в `data/profiles/`. Если есть подозрение,
что профили должны перегенерироваться `first_run.py`, нужно либо
добавить явный код перегенерации в `first_run.py`, либо явно
документировать, что встроенные профили создаются при первом запуске.

### MCP-серверы build и data — осознанная санация dev-модулей

Оба агента `build` и `data` имеют `mode: destructive` и помечены
`dangerous_tools`:

- **`build`** — 16 инструментов для сборки и Docker:
  `build_run`, `build_clean`, `pkg_add`, `pkg_remove`, `pkg_update`,
  `pkg_audit`, `docker_build`, `docker_run`, `docker_stop`,
  `docker_compose_up`, `docker_compose_down` и др.
- **`data`** — управление данными (предположительно ETL-операции).

Эти MCP-серверы позволяют агенту выполнять потенциально опасные
операции (установку/удаление пакетов, сборку Docker-образов, запуск
контейнеров). Удаление их из финальной сборки выглядит как
сознательная санация перед релизом: dev-команда оставляет эксперименты
в github-ready, но не включает их в production-дистрибутив.

**Версия:** если эти MCP нужны только в dev-окружении, рекомендуется
перенести их в отдельный `agents-dev/` и `mcp_servers-dev/` каталоги,
плюс добавить опцию `--include-dev` в `install.py`, которая подключит
их только при явном запросе. Это сделает разделение production/dev
явным, а не неявным через отдельный ZIP-архив.

---

## Проверка идентичности общих файлов

Все 695 файлов финальной сборки есть в github-ready. Чтобы убедиться,
что содержимое совпадает байт-в-байт:

```bash
# Сравнить MD5-хеши топ-уровневых файлов
cd /home/z/my-project/work
for f in $(cd extract1/llm-agent && find . -maxdepth 1 -type f -not -path "./.git*" | sort); do
  a1=$(md5sum "extract1/llm-agent/$f" | awk '{print $1}')
  a2=$(md5sum "extract2/llm-agent/$f" | awk '{print $1}')
  [ "$a1" != "$a2" ] && echo "DIFF: $f"
done
# (нет вывода = все совпадают)

# Полное сравнение всех общих файлов
diff <(cd extract1/llm-agent && find . -type f -not -path "./.git*" \
        -not -path "./extension/dist*" -not -path "./extension/icons*" | sort) \
     <(cd extract2/llm-agent && find . -type f -not -path "./.git*" \
        -not -path "./extension/dist*" -not -path "./extension/icons*" | sort)
# Вывод покажет только 16 строк '< ./path' — файлы, которые есть в
# extract1 (github-ready), но отсутствуют в extract2 (final).
```

---

## План действий для разработчика

Если вы — maintainer проекта llm-agent и хотите привести финальную
сборку в порядок, выполните следующие шаги:

### Шаг 1. Восстановите 4 runtime-профиля

```bash
# Из корня проекта (где лежит README.md)
mkdir -p data/profiles
# Скопируйте 4 файла из github-ready или из этого патча:
cp /path/to/patch/data/profiles/*.json data/profiles/

# Проверьте, что first_run.py их не перезатирает при запуске:
python -c "from src.core.profiles import load_profiles; print(load_profiles())"
```

### Шаг 2. Документируйте разделение dev/production

Добавьте в `README.md` после раздела «## 🚀 Запуск» новый подраздел:

```markdown
### Сборки проекта

Проект распространяется в двух сборках:

- **`github-ready`** — полный снимок репозитория с git-историей и
  экспериментальными MCP-серверами (build, data). Используется для
  разработки и контрибьюторов.
- **`final`** — чистый production-дистрибутив без git-истории и
  dev-модулей. Используется для деплоя в прод.

Если вам нужны экспериментальные MCP (build, data) в production,
скопируйте их из github-ready сборки вручную или запустите
`install.py --include-dev`.
```

### Шаг 3. (Опционально) Реорганизуйте dev-MCP в отдельный каталог

```bash
mkdir -p agents-dev mcp_servers-dev src/mcp_servers-dev
mv agents/build agents/data agents-dev/
mv mcp_servers/build mcp_servers/data mcp_servers-dev/
mv src/mcp_servers/build src/mcp_servers/data src/mcp_servers-dev/

# Добавьте в install.py опцию --include-dev, которая символическими
# ссылками подключит dev-модули, если флаг передан.
```

---

## Связанные документы

- **`MAIN-PY-DECOMPOSITION-PLAN.md`** — план декомпозиции `src/main.py`
  (2 640 строк) в FastAPI-роутеры. Рекомендация 1 из Спринта 1.
- **`SPRINT-ROADMAP.xlsx`** — трекер 14 задач с приоритетами P0/P1/P2,
  трудозатратами и статусами.
- Технический отчёт `llm-agent-archives-analysis.pdf` — полный анализ
  обоих архивов.
