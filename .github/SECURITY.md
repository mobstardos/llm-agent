# Security Policy — llm-agent

> Рекомендация 11 из технического анализа (Sprint 5, P2).

## Поддерживаемые версии

| Версия | Статус | Поддержка безопасности |
|---|---|---|
| `2026-09-25` (final) | ✅ Current | Активно патчим |
| `2026-09-25` (github-ready) | ⚠️ Snapshot | Только через cherry-pick в main |
| старее | ❌ EOL | Обновитесь |

## Сообщить об уязвимости

**НЕ открывайте public GitHub Issue для security-уязвимости.**

### Каналы связи

1. **Email (предпочтительно):** отправьте описание на
   `security@[domain]` (см. профиль GitHub-организации).
   Шаблон письма — ниже.

2. **GitHub Security Advisories (альтернатива):** откройте
   приватный advisory через `Security` → `Advisories` → `New advisory`.
   Это позволяет обсуждать детали приватно до публикации.

3. **PGP-шифрование (опционально):** скачайте публичный ключ с
   `https://[domain]/security.pub`. Fingerprint: `[заполнить после
   генерации ключа]`.

### Что указать в отчёте

- **Описание уязвимости** — что именно exploitable, в каком сценарии.
- **Затронутые компоненты** — конкретные файлы/модули
  (`src/mcp_servers/debug/server.py`, `src/llm_client.py` и т. д.).
- **Версия** — из `requirements.txt` или git commit hash.
- **Шаги воспроизведения** — PoC-скрипт или минимальные шаги.
- **Возможное влияние** — RCE / data leak / DoS / privilege escalation.
- **Предлагаемое исправление** (опционально).

### Что произойдёт дальше

1. **Подтверждение (≤ 72 часа):** мы подтверждаем получение отчёта и
   назначаем triage-приоритет.
2. **Triage (≤ 7 дней):** проверяем, оцениваем по CVSS.
3. **Fix development:** готовим патч в приватной ветке. Согласуем с
   вами timing публикации (по умолчанию — 90 дней disclosure).
4. **Coordinated disclosure:** публикуем advisory + CVE (если
   применимо) одновременно с релизом патча. Благодарим вас в advisory
   (если согласны).

## Угрозовая модель проекта

llm-agent — **локальный агент, который модифицирует файлы и БД от
имени пользователя**. Основные векторы риска:

| Вектор | Где защищено | Возможные проблемы |
|---|---|---|
| **Произвольное исполнение кода** | `src/mcp_servers/debug/server.py` — `exec()` через `cProfile.run()`. Защита: `mode: destructive` + approval-gate (HITL). | Если approval-gate обойти (например, через supervisor с disabled policies) — RCE. |
| **Доступ к секретам** | `.env` содержит `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `PG_APP_PASSWORD`. Код НЕ читает `~/.ssh` или системные keychain — только `.env`. | Если `.env` попал в git — секреты утекли. Запретить через `.gitignore`. |
| **Доступ к БД** | Агенты БД (`mysql`, `postgres`) работают в режиме `read` по умолчанию. Поля `dangerous_tools: [execute_write_query, execute_migration, drop_table, delete_rows]` требуют confirmation. | Если пользователь включил `mode: destructive` в профиле `development.json` — опасные операции без подтверждения. |
| **FileSystem sandbox escape** | `src/mcp_servers/filesystem/server.py` — все пути проверяются против `PROJECT_ROOT`. | Symlink на `/etc/passwd` — проверьте, что `resolve()` разворачивает симлинки до проверки. |
| **Shell-команды** | `src/mcp_servers/shell/server.py` — `subprocess.run(shell=False)` (см. отчёт §8: 0 `shell=True`). Команды исполняются через `subprocess.run(["/bin/bash", "-c", cmd])` — но `shell=True` нет. | Если в `args` прокидывается пользовательский ввод без санитизации — command injection через `-c` аргумент. |
| **Browser extension** | `extension/` — thin client, не исполняет arbitrary JS от сервера. WS-протокол с проверкой origin. | Если WS endpoint принимает соединения с любого origin (CORS misconfigured) — XSS через extension. |

## Известные risk-точки (см. отчёт §8)

- **4 `exec()` в `src/mcp_servers/debug/server.py`** — легитимно для
  debug-MCP (профилирование пользовательских скриптов), но требует
  изоляции (Docker/Pyodide) в production-деплое.
- **4 `assert` в проде** (см. рекомендация 5 — Sprint 2 патч):
  `src/llm_client.py:184`, `src/mcp_servers/lsp/client.py:111/160`.
- **452 `print()` в коде** — не security-риск сам по себе, но может
  случайно залогировать секреты в stdout. Заменить на logging с
  redaction (см. рекомендация 12 — Sprint 5 патч).
- **Hardcoded passwords в `scripts/test_bridge.py`** — fake credentials
  для тестов. Не критично, но ruff/bandit будут ругаться.

## Hardening-чеклист для production

Перед deploy в production:

- [ ] `.env` файл не в git (`git status .env` должен показать "untracked" или быть в `.gitignore`).
- [ ] Создан отдельный PostgreSQL-пользователь с минимальными правами (только на `llmagent` database, без SUPERUSER).
- [ ] `development.json` профиль НЕ активен в production.
- [ ] Включён `production.json` профиль — `shell` и `git` агенты отключены.
- [ ] `BACKUP_ENABLED=true` и `BACKUP_KEEP_LAST≥7`.
- [ ] `PG_RETENTION_DAYS>0` — авто-ретенция зеркал в PG.
- [ ] Docker-контейнер запускается от непривилегированного пользователя (см. Dockerfile, USER llmagent).
- [ ] Healthcheck эндпоинт `/api/db/health` отвечает 200 в течение 60 секунд после старта.
- [ ] Мониторинг логов на `ERROR`/`CRITICAL` настроен (Promtail → Loki или ELK).
- [ ] SSL/TLS терминирован через nginx (см. `nginx.conf`) — порт 80 проксирует на 8000.
- [ ] Rate-limiting на `/ws` и `/api/*` эндпоинты (через nginx limit_req или slowapi в FastAPI).

## Safe Harbor (благодарность исследователям)

Мы поддерживаем responsible disclosure и **не будем преследовать**
исследователей, которые:

- Не эксплуатируют уязвимость дальше подтверждения концепции.
- Не раскрывают детали публично до coordinated disclosure.
- Не нарушают закон и чужую конфиденциальность.
- Сообщают через указанные каналы.

Мы будем публично благодарить в advisory и (по запросу) в
RELEASE_NOTES тех, кто помог улучшить безопасность проекта.

## Контакт

- **Security email:** `security@[your-domain]`
- **GitHub Security Advisories:** [ссылка появится после настройки organisation]
- **PGP-ключ:** `https://[your-domain]/security.pub`
- **Response time:** 72 часа на подтверждение, 7 дней на triage.

## История изменений

- `2026-09-25` — первичная версия политики (Sprint 5 патч).
