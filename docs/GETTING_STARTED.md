# 🚀 Запуск и настройка LLM Agent — подробная инструкция

> Пошаговое руководство: от чистого Windows/Linux до работающего мультиагентного
> помощника с веб-интерфейсом. Для типовых проблем сразу смотрите
> [FAQ.md](FAQ.md), для полного перечня возможностей — [CAPABILITIES.md](CAPABILITIES.md),
> для карты «что за что отвечает» в коде — [STRUCTURE.md](STRUCTURE.md).
>
> В проекте есть **интерактивный курс**: запустите систему и откройте
> <http://127.0.0.1:8000/guide> (или кнопка 🎓 в шапке чата).

---

## 1. Что понадобится

| Компонент | Требование | Зачем |
|---|---|---|
| ОС | Windows 10/11 x64, Linux, macOS | основная разработка — под Windows |
| Python | **3.11–3.14**, обычная сборка (не из Microsoft Store!) | установщик требует ≥ 3.11 |
| Интернет | нужен только на время установки и для облачных моделей | pip, модели |
| Node.js ≥ 18 | опционально | только для qwenproxy (браузерный путь Qwen) |
| Docker Desktop | опционально | для PostgreSQL в контейнере |
| Ollama | опционально | локальные модели без облака |
| Git | опционально | удобнее обновлять проект |

⚠️ **Python из Microsoft Store не подходит** — это заглушка, которая молча
открывает магазин вместо работы. Ставьте с <https://www.python.org/downloads/>
и отмечайте галочку **«Add python.exe to PATH»**.

> 📝 **Нюанс Python на Linux:** в зависимостях есть `pgserver` (встроенный
> PostgreSQL для самопроверок) — у него колёса только до Python 3.12, поэтому
> на 3.13+ установщик его просто пропустит. На работу системы это не влияет:
> полноценный PostgreSQL подключается отдельно (раздел 7), а Windows этот
> пакет не касается вовсе.

Свободно на диске: ~2 ГБ для зависимостей; +1–3 ГБ, если включите эмбеддер
модели bge-m3; +2 ГБ VRAM, если планируете локальную Ollama-модель
(проект рассчитан на одну маленькую `qwen2.5:1.5b`).

---

## 2. Быстрый старт (три команды)

Самый надёжный путь — установщик на Python (он не зависит от причуд cmd.exe):

```bat
:: Windows
python install.py
run.bat
```

```bash
# Linux / macOS
python3 install.py
bash run.sh
```

Установщик сам: проверит Python, создаст `.venv`, поставит зависимости,
спросит порт и PostgreSQL, запишет `.env`, накатит схему БД и прогонит
смоук-тест сервера. После `run.bat` откройте <http://127.0.0.1:8000>.

---

## 3. Способы установки

### 3.1. `python install.py` — рекомендуемый (кроссплатформенный)

```bat
python install.py          :: интерактивно (порт, PostgreSQL, embedder)
python install.py --auto   :: тихо, всё по умолчанию (порт 8000, PG пропустить)
```

Что происходит по шагам:

1. **Проверка Python** (нужен ≥ 3.11). Free-threaded сборки (3.13t/3.14t)
   распознаются автоматически — под них берётся усечённый набор зависимостей
   `requirements-freethreaded.txt` (без lancedb/tree-sitter/whisper и т.п.).
2. **Создание `.venv`** в папке проекта.
3. **Установка зависимостей** — живой вывод pip; на Python 3.14+ при нехватке
   колёс установщик сам повторит на усечённом наборе.
4. **Вопросы**: порт (по умолчанию 8000), PostgreSQL
   (`1` — Docker, `2` — внешний DSN, `3` — пропустить), embedder памяти
   (`auto` — bge-m3 или быстрый hashing, `hash` — мгновенно без загрузок).
5. **Запись `.env`** — существующий файл не перезаписывается.
6. **Первичная настройка** (`first_run.py --defaults`): каталоги `data/`,
   `logs/`, локальные SQLite-базы.
7. **Схема PostgreSQL** (`scripts/init_db.py`), если выбран PG.
8. **Смоук-тест**: сервер стартует на выбранном порту, установщик ждёт
   HTTP 200 до 2 минут (первый старт долгий — поднимаются 38+ MCP-серверов),
   затем корректно его останавливает.

### 3.2. `install.bat` — для Windows «в один клик»

Двойной клик по `install.bat`. Делает то же, что `install.py`, плюс
интерактивно спрашивает куки DeepSeek. Работает и в тихом режиме:
`install.bat --auto`.

Поддержан фолбэк: если полный набор зависимостей не собрался под Python 3.14+,
автоматически повторяет установку на `requirements-freethreaded.txt`.

### 3.3. Из git-клона (репозиторий на GitHub)

```bash
git clone https://github.com/mobstardos/llm-agent.git
cd llm-agent
python install.py      # далее — как в 3.1
bash run.sh / run.bat
```

### 3.4. Вручную

```bash
python -m venv .venv
# Windows:
.venv\Scripts\python -m pip install -r requirements.txt
# Linux/macOS:
.venv/bin/python -m pip install -r requirements.txt

python first_run.py --defaults   # каталоги + базы + .env
python run.py                    # запуск
```

### 3.5. Browser MCP (опционально)

Нужен только веб-агенту (открытие страниц, скриншоты):

```bat
install_playwright.bat          :: Windows (поставит playwright + chromium)
```

```bash
pip install playwright && playwright install chromium
```

---

## 4. Первый запуск и мастер настройки AI

При первом `python run.py` (или `run.bat`) автоматически стартует мастер
`first_run.py`. Он спрашивает:

1. **Основной путь — браузер/куки DeepSeek** (рекомендуется, без API-ключей):
   - откройте <https://chat.deepseek.com> и войдите в аккаунт;
   - `F12 → Application → Cookies → https://chat.deepseek.com`;
   - скопируйте `ds_session_id` (обязателен), `smidV2`, `.thumbcache_6b2e5483…`,
     `userToken` из заголовка `Authorization` (опциональны).
   - Значения лягут в `.env`: `DEEPSEEK_DS_SESSION_ID`, `DEEPSEEK_SMIDV2`,
     `DEEPSEEK_THUMBCACHE`, `DEEPSEEK_AUTH_TOKEN`.
   - Ещё удобнее — расширение браузера (раздел 8): куки собираются в один клик
     и попадают в систему через веб-интерфейс.
2. **Второстепенный путь — OpenAI-совместимый API** (фолбэк роутера):
   готовые пресеты: DeepSeek API, VseGPT, ProxyAPI (работают из РФ),
   Ollama (локально), OpenAI (из РФ даст 403 region). Мастер может сразу
   проверить связь (`--check-llm`-логика).
3. **qwenproxy** — браузерный роутер Qwen (нужен Node.js):
   `npm install -g qwenproxy-cli`, затем запустите `qpx` и во вкладке
   `[5] Accounts` нажмите `A` и введите **email и пароль своего аккаунта
   chat.qwen.ai** (или `B` — пакетно, по строке `email:password`).
   Можно включить автозапуск вместе с сервером (`QWENPROXY_AUTO_START=true`).
   **Без Node.js вообще** — путь «только куки»: расширение Bridge → 🔑 Qwen
   (или 🔑 DeepSeek) → войти в веб-чат; модели «веб-чат по кукам» появятся
   в селекте сразу (подробно: docs/providers.md).

Повторная настройка AI в любой момент:

```bat
python first_run.py --reconfigure-ai   :: заново спросить настройки AI
python first_run.py --check-llm        :: только проверить связь с провайдером
python first_run.py --reset            :: сбросить и пройти мастер заново
```

`--check-llm` знает про оба пути: если в `.env` есть куки — покажет строку
«Основной путь — браузер/куки: DeepSeek (deepseek_web)» и при молчащем
7936 не считает это ошибкой (7936 — необязательный второстепенный роутер).
Также `run.py` заранее проверяет занятость порта: при работающем старом
экземпляре подскажет, как его остановить (свежий `.env` подхватывает
только перезапуск).

Маркер «проект настроен» — `data/.initialized`.

---

## 5. Ежедневный запуск

| Способ | Команда | Примечание |
|---|---|---|
| Windows, двойной клик | `run.bat` | сам создаст venv и зависимости при первом запуске; при ошибке окно **не закроется** — будет пауза и причина |
| Windows, консоль | `.venv\Scripts\python run.py` | флаги: `--skip-checks`, `--skip-setup`, `--port NNNN`, `--clean-ports` (убить зомби-процессы на портах перед стартом) |
| Linux/macOS | `bash run.sh` | то же самое |
| Из корня | `python main.py` | шим на `run.py` |
| Готовый exe | `dist\llm-agent.exe` | собирается `build_exe.bat` (PyInstaller) |

Что вы увидите при старте:

```
┌─ Python ────────────  ✓ Python 3.14.3, ✓ venv
┌─ Зависимости ───────  ✓ fastapi, ✓ uvicorn, ... (! lancedb — опционально)
┌─ PostgreSQL ────────  ✓ подключён (via autodetect) / ! не найден — SQLite
┌─ QwenProxy ─────────  ✓ работает на 127.0.0.1:7936
┌─ Запуск сервера ────  http://127.0.0.1:8000
```

Первый старт занимает 1–2 минуты (запуск MCP-серверов, создание векторных
индексов). Последующие — быстрее.

Останов: `Ctrl+C` в консоли.

---

## 6. Настройка: где что крутится

Настройки живут в трёх местах:

1. **`.env`** — окружение и тяжёлые параметры (читается один раз при старте).
2. **`config/*.yaml`** — модели (`models.yaml`), память (`memory.yaml`),
   алертинг, извлечение документов и т.д.
3. **Веб-интерфейс (⚙️ Настройки)** — «горячая» конфигурация: реестр агентов и
   MCP, политики, профили, бэкапы, синонимы, AGE, CDC, диагностика. Изменения
   применяются без перезапуска и сохраняются в `config/runtime.yaml`.

### 6.1. Ключевые переменные `.env`

Полный список с комментариями — `.env.example` (~300 строк). Самое нужное:

| Группа | Переменная | По умолчанию | Описание |
|---|---|---|---|
| Сервер | `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8000` | адрес и порт веб-интерфейса |
| AI (API) | `LLM_BASE_URL` | `http://127.0.0.1:7936/v1` | OpenAI-совместимый endpoint |
| | `LLM_API_KEY` | — | ключ провайдера |
| | `LLM_MODEL` | `qwen3.8-max` | модель по умолчанию |
| | `LLM_FALLBACKS` | из `models.yaml` | цепочка фолбэков через запятую |
| AI (qwenproxy) | `QWENPROXY_ENABLED` / `QWENPROXY_AUTO_START` | `true`/`false` | браузерный роутер Qwen |
| Куки DeepSeek | `DEEPSEEK_DS_SESSION_ID` и др. | — | браузерный путь DeepSeek (раздел 4) |
| Куки Qwen | `QWEN_COOKIE` | — | кука веб-чата Qwen |
| Память | `AGENT_MEMORY_EMBEDDER` | `auto` | `auto` / `hash` / `model` |
| Ollama | `OLLAMA_ENABLED`, `OLLAMA_URL`, `OLLAMA_MODEL` | `true`, `http://127.0.0.1:11434`, `qwen2.5:1.5b-instruct` | локальные микрозадачи и модели |
| Микрозадачи | `LOCAL_MICRO_TASKS_ENABLED` | `1` | заголовки/теги/дайджест на локальной модели |
| PostgreSQL | `PG_ENABLED`, `DATABASE_URL` или `PG_APP_*` | — | зеркала журнала/сессий, память агентов, аналитика |
| | `PG_REPLICATE` | `1` | `0` — выключить репликацию (тихий режим без PG) |
| Векторные хранилища | `PRIMARY_VECTOR_STORE` и др. | `lancedb` | `lancedb` / `postgres` / `files` |
| Бэкапы | `BACKUP_ENABLED`, `BACKUP_INTERVAL_HOURS`, `BACKUP_KEEP_LAST` | `true`, `6`, `7` | авто-бэкапы баз |
| Журнал | см. `docs/JOURNAL.md` | — | ретенция, реплей |
| Кэш | `CACHE_ENABLED`, `CACHE_TTL` | `true`, `86400` | кэш ответов LLM |
| Петля агента | `LOOP_MAX_ITERATIONS`, `LOOP_MAX_TOKENS`, `LOOP_HARD_LIMIT` | — | бюджет итераций/токенов/времени |
| Кластер | `CLUSTER_ENABLED`, `REDIS_URL` | `false` | мульти-инстанс через Redis-шину |

После правки `.env` перезапустите сервер.

### 6.2. Настройки прямо в веб-интерфейсе

Кнопка **⚙️** в шапке — 14 вкладок: Система (кэш, обогащение, дайджест),
Агенты, MCP, Capabilities, Политики, Бэкапы, Синонимы, AGE, CDC, История,
Профили, Аудит, Rollback, Диагностика (snapshot + промпт оркестратора).
Выбор модели — в шапке чата (кнопка с именем модели): qwenproxy / DeepSeek /
Ollama / OpenAI / OpenRouter / SiliconFlow / VseGPT / ProxyAPI / LM Studio /
Anthropic / Gemini / Groq / Mistral; выбор сохраняется и переживает
перезагрузку. Ссылка на папку проекта — кнопка 📁.

### 6.3. `config/*.yaml` — карта конфигов

Эти файлы читаются при старте сервера — правки вступают в силу после
перезапуска (в отличие от «горячих» настроек веб-интерфейса из 6.2,
которые живут в `config/runtime.yaml` и применяются на лету).

| Файл | За что отвечает | Что настраивается |
|---|---|---|
| `models.yaml` | реестр провайдеров и моделей, модель по умолчанию, фолбэки | `default: qwen3.8-max`; блок `providers:` — как расширять, см. 6.4 |
| `memory.yaml` | рабочая, эпизодическая и семантическая память | `working.max_context_tokens` (32000) и пороги компакции/усечения (0.60/0.80), бюджет контекста по секциям (system/profile/recent/summary/recall); ретенция эпизодов: сырые сообщения 90 дней, события 180, саммари 730; автосаммари в конце сессии и ежедневно в 03:00; профиль проекта `data/project_profile.yaml` с авто-регенерацией раз в 24 ч |
| `settings.yaml` | общие настройки: какие агенты включены по умолчанию, команды запуска MCP-серверов | `agents.enabled_by_default` / `disabled_by_default`; `mcp_servers.<имя>.command/args/env` (например, куки DeepSeek пробрасываются в MCP через `env:`) |
| `alerting.yaml` | правила алертов (баннер в UI, запись в аудит-лог) | `db_unavailable` (critical), `llm_slow` (warn, p95 > 30 с), `mcp_crash_loop` (warn) |
| `extraction.yaml` | извлечение текста из документов | лимиты: файл ≤ 100 МБ, 100 000 знаков, таймаут 60 с, 4 потока; кэш `data/extraction.sqlite` (30 дней); цепочки fallback: pdf → `pdf_text` → `pdf_ocr`, image → `image_vision` → `image_ocr` → `image_metadata` |
| `litellm.example.yaml` | готовый пример шлюза LiteLLM для провайдеров с нативным не-OpenAI API | см. 6.4 |

Значения в YAML поддерживают подстановку переменных из `.env`:
`${VAR}` или `${VAR:default}` — так, например, `base_url` провайдера
qwen подхватывает `QWENPROXY_URL`.

### 6.4. Добавление своего провайдера моделей

Любой OpenAI-совместимый endpoint подключается **без изменения кода** —
три строки в `config/models.yaml` плюс ключ в `.env`:

```yaml
providers:
  myprovider:
    name: Мой провайдер
    base_url: https://api.example.com/v1   # поддерживает ${VAR:default}
    api_key_env: MYPROVIDER_API_KEY        # имя переменной .env с ключом
    # local: true                          # бейдж «локально» в UI
    # docs_url: https://…                  # подсказка «где взять ключ»
```

После перезапуска `/api/models` сам опросит `…/v1/models`, и модели
появятся в селекте чата группой провайдера. Формат выбора —
`provider/model` (например `openrouter/deepseek-chat`) или просто
`model`, если имя уникально. Подробно — [providers.md](providers.md).

**Провайдеры с нативным не-OpenAI API** (Bedrock, Vertex AI, GigaChat…)
подключаются через шлюз LiteLLM:

```bash
pip install 'litellm[proxy]'
litellm --config config/litellm.example.yaml --port 4000
```

затем в `models.yaml` добавляется провайдер с
`base_url: http://127.0.0.1:4000/v1` и `api_key_env` на master-key.
Список готовых моделей-примеров — в самом `config/litellm.example.yaml`.

### 6.5. Порты и сервисы

| Порт | Сервис | Чем настраивается |
|---|---|---|
| 8000 | веб-интерфейс и API | `WEB_HOST` / `WEB_PORT` |
| 7936 | qwenproxy (браузерный роутер Qwen) — необязателен | `QWENPROXY_URL` |
| 11434 | Ollama (локальные модели) | `OLLAMA_URL` |
| 5432 | PostgreSQL основной (pgvector) | `DATABASE_URL` / `PG_APP_*` |
| 5433 | Apache AGE (граф, опционально) | `AGE_DSN` |
| 9092 | Kafka (CDC, опционально) | `KAFKA_BOOTSTRAP_SERVERS` |
| 8080 | Kafka UI | `docker-compose.yml` |
| 6379 | Redis (кластерный режим) | `REDIS_URL` |
| 4000 | LiteLLM-шлюз (если запущен) | флаг `--port` при старте |

Занятый порт — самая частая причина «сервер не стартует»: `run.py`
проверяет это заранее и подсказывает, как остановить старый экземпляр;
радикальное средство — `python run.py --clean-ports`.

---

## 7. PostgreSQL (опционально, но полезно)

Без PostgreSQL система полноценно работает на локальных файлах
(SQLite + LanceDB). PG нужен для: долговременных зеркал журнала/сессий/планов,
памяти агентов, AGE-графа, аналитики, CDC.

**Docker (проще всего):**

```bash
docker compose up -d postgres     # поднимет контейнер из docker-compose.yml
python scripts/init_db.py         # идемпотентно накатит схему (init + journal + ops + аналитика)
python scripts/init_db.py --check # проверить, что схема на месте
```

> Контейнер из docker-compose накатывает схему сам при первом создании
> тома (`db/*.sql` смонтированы в `docker-entrypoint-initdb.d`), так что
> `init_db.py` здесь — идемпотентная доводка и проверка, а не ритуал.

**Apache AGE и Kafka (опционально):** в том же `docker-compose.yml`
уже описаны графовая база и CDC-шина — поднимаются отдельно:

```bash
docker compose up -d age-postgres      # Apache AGE, порт 5433
docker compose up -d kafka kafka-ui    # Kafka 9092 + UI на http://localhost:8080
```

и включаются в `.env` (по умолчанию оба выключены):

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `AGE_ENABLED`, `AGE_DSN`, `AGE_GRAPH` | `false`, `…llmagent:secret@localhost:5433/llmagent`, `llm_graph` | property-граф: сущности и связи для долговременной памяти |
| `KAFKA_ENABLED`, `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_TOPIC_PREFIX` | `false`, `localhost:9092`, `llmagent.cdc` | поток CDC-событий в Kafka-топики |

⚠️ **AGE не работает нативно на Windows** — только Docker/WSL2
(контейнерный путь выше работает на любой ОС).

**Внешний сервер** — пропишите в `.env` либо `DATABASE_URL=postgres://user:pass@host:5432/db`,
либо набор `PG_APP_HOST/PG_APP_PORT/PG_APP_USER/PG_APP_PASSWORD/PG_APP_DATABASE`.

**Автодетект (Windows-friendly):** при старте `run.py` служба PostgreSQL
ищется автоматически: если пароль не подходит — пробуется словарь дефолтных
паролей, затем (в TTY) спрашивается пароль администратора; создаётся роль
`llmagent` и применяется схема. Подробности: `docs/DATABASE.md`.

Если PG не нужен — в `.env`: `PG_ENABLED=false` и `PG_REPLICATE=0`
(в логе будет одна строка вместо периодических предупреждений).

---

## 8. Расширение браузера (Bridge)

Расширение связывает чат с браузером: пробуждает агента кнопкой
(`Alt+Shift+B`), суммаризирует открытую страницу из контекстного меню и
**собирает куки веб-чатов DeepSeek/Qwen** в один клик (без ручного F12).

Готовые сборки лежат в `extension/dist/`:

| Браузер | Файл | Установка |
|---|---|---|
| Chrome/Edge/Яндекс | `extension/dist/llm-agent-bridge-chromium-1.2.0.zip` | распаковать → `chrome://extensions` → «Загрузить распакованное» → папка `extension/chromium` |
| Firefox (постоянно) | подписанный `.xpi` из `extension/dist/signed/` | `python scripts/sign_firefox.py --api-key … --api-secret …` → открыть xpi в Firefox |
| Firefox (отладка) | папка `extension/firefox` | `about:debugging` → «Загрузить временное дополнение» |

После установки укажите адрес сервера в настройках расширения
(по умолчанию `http://127.0.0.1:8000`) и проверьте секцию «Куки веб-чатов»
в попапе. Куки можно просматривать/удалять и через веб-интерфейс — вкладка
Bridge в настройках, API `/api/bridge/cookies`.

---

## 9. Обновление, сброс, удаление

**Обновить проект из git:**

```bash
git pull
del .venv\.installed        # Windows (в bash: rm .venv/.installed) — заставит установщик перепроверить зависимости
python install.py
python -m src.cli migrate           # сухой прогон: покажет устаревшие декларации
python -m src.cli migrate --write   # поднять schema_version в agents/*/agent.yaml и mcp_servers/*/server.yaml
bash run.sh                         # Windows: run.bat
```

`migrate` без `--write` ничего не меняет — только показывает, что
обновится. После обновления проверьте схему PostgreSQL:
`python scripts/init_db.py --check` (накат идемпотентный).

**Сбросить AI-настройки:** `python first_run.py --reconfigure-ai`.

**Полный сброс настроек:** `python first_run.py --reset` (пересоздаёт маркер,
спросит всё заново; существующий `.env` значения мастером перечитываются).

**Начать данные с нуля:** остановите сервер и удалите `data/` (базы воссоздадутся
при следующем старте) — делайте бэкап (`⚙️ → Бэкапы → Создать бэкап`).

**Удалить проект:** остановите сервер, удалите папку (`.venv` и `data/`
лежат внутри, наружу ничего не пишется; `PROJECT_ROOT` в `.env` указывает
на папку проекта).

---

## 10. Диагностика — если что-то не так

| Что проверить | Как |
|---|---|
| Связь с LLM-провайдером | `python first_run.py --check-llm` |
| Сервер жив | <http://127.0.0.1:8000/api/db/status> → `{"ok": true}` |
| Модели/провайдеры | кнопка модели в шапке → «🔄 Обновить»; API `/api/models` покажет причину недоступности каждого |
| Схема PostgreSQL | `python scripts/init_db.py --check` |
| Устаревшие декларации (после git pull) | `python -m src.cli migrate` → `--write` |
| AGE / Kafka не подключаются | `docker compose ps`; в `.env` включены `AGE_ENABLED` / `KAFKA_ENABLED`? |
| Занятый порт | `python run.py --clean-ports` (или смените `WEB_PORT`) |
| Автодетект PG | `/api/db/autodetect` в веб-интерфейсе |
| Журнал и откат | вкладки «История» / «Rollback» в настройках |
| Смоук-тест после установки | запускается автоматически `install.py`; лог — `.install_smoke.log` |
| Логи | папка `logs/` |

Подробные разборы типовых ошибок — [FAQ.md](FAQ.md).
