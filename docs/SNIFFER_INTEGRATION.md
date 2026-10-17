# Интеграция UniversalSniffer v3.1 в llm-agent

UniversalSniffer — прозрачный **TCP-прокси сниффер** на чистой стандартной
библиотеке Python (0 внешних зависимостей): перехватывает трафик между
клиентами и сервером, разбирает протоколы плагинами, показывает живую картину
в веб-панели, пишет JSONL / SQLite / PCAP / CSV и поднимает тревоги по правилам.

В llm-agent сниффер встроен на трёх уровнях:

1. **Вендоренный движок** — `src/sniffer/` (копия UniversalSniffer v3.1,
   запускается как `python -m src.sniffer`).
2. **MCP-сервер `sniffer`** — `src/mcp_servers/sniffer/server.py`: 16
   инструментов (HTTP-клиент к панели + управление жизненным циклом процесса).
3. **Агент `sniffer`** — `agents/sniffer/`: промпт и правила маршрутизации
   (роутер отдаёт ему запросы про «сниффер/трафик/пакеты/дамп/порты»).

## Архитектура

```
┌──────────────┐   WebSocket    ┌─────────────────────────────┐
│  Веб-чат UI  │◄──────────────►│  FastAPI (src/main.py)      │
└──────────────┘                │  оркестратор → агент sniffer│
                                └──────────────┬──────────────┘
                                               │ MCP stdio
                                ┌──────────────▼──────────────┐
                                │ MCP-сервер sniffer           │
                                │ src/mcp_servers/sniffer/     │
                                │  • HTTP-клиент (urllib, 5 c) │
                                │  • lifecycle: Popen + pid    │
                                └───────┬─────────────┬───────┘
                     subprocess spawn   │             │  HTTP API :9500
                 python -m src.sniffer  │             │  /api/status /api/stats
                                ┌───────▼───────┐     │  /api/packets /api/alerts …
                                │ Engine        │     │
                                │ (TCP-прокси)  │     │
                                │  listen→target│     │
                                └───┬───────┬───┘     │
                              TX/RX │       │ parsers │
                          клиенты◄──┘       ▼         ▼
                          АЗС/1С/клиент   ParserRegistry  webserver.py
                          (трафик         thrift, remote_server, modbus,
                          ретранслируется json_http, hexdump
                          всегда)              │
                                               ▼
                                    exports: JSONL/SQLite/PCAP/CSV → capture/
                                    alerts: правила тревог → alerts.jsonl + UI
```

Ключевые гарантии движка: данные **ретранслируются всегда** (разбор — чистое
наблюдение, ошибка парсера не рвёт канал); автодетект протокола по первым
байтам; partial-кадры накапливаются (TCP-границы не гарантированы).

## Файлы интеграции

| Файл | Что это |
|---|---|
| `src/sniffer/__init__.py` | пакет, `__version__ = "3.1.0"` |
| `src/sniffer/__main__.py` | CLI: `python -m src.sniffer --config … --ports …` (адаптированный sniffer.py) |
| `src/sniffer/sniffcore/` | движок: engine.py (прокси), hub.py (кольцо пакетов/сессии/статистика), webserver.py (HTTP API + SSE), alerts.py, exports.py, console.py, detection.py, winservice.py (Windows-служба, как в оригинале) |
| `src/sniffer/parsers/` | парсеры: hexdump, json_http, modbus, remote_server, thrift (+ плагины из `plugins/`) |
| `src/sniffer/webui/` | веб-панель (index.html, app.js, style.css) |
| `src/sniffer/config.json` | конфиг по умолчанию (порты 9000→9001, 9010→9011, панель 9500) |
| `src/sniffer/README.md` | README оригинального проекта |
| `src/sniffer/selftest.py` | самопроверка: эхо-сервер + in-process прокси + проверка разбора |
| `src/mcp_servers/sniffer/server.py` | MCP-сервер (16 инструментов) |
| `src/mcp_servers/sniffer/__init__.py` | пакет |
| `mcp_servers/sniffer/server.yaml` | декларация MCP (danger-уровни, process-политика) |
| `agents/sniffer/agent.yaml` + `prompt.md` + `user.md` | декларация агента |
| `config/settings.yaml` | секции `mcp_servers.sniffer` и `mcp_servers.onec_designer_tools` (env через `${VAR}`) |
| `docs/SNIFFER_INTEGRATION.md` | этот документ |

Данные пишутся в `data/sniffer/` (переменная `USNIFF_ROOT`, которую MCP-сервер
ставит при запуске subprocess): `capture/` (jsonl/db/pcap/reports),
`plugins/`, `data/sniffer.pid`, `data/sniffer-console.log`.

## Инструменты MCP ↔ HTTP API панели

| MCP-инструмент | HTTP API панели | danger |
|---|---|---|
| `sniffer_status` | GET /api/status + локальный процесс (pid, uptime) | read |
| `sniffer_start` (ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ) | — (Popen: `python -m src.sniffer --config … [--ports …] [--web-port …]`, cwd = PROJECT_ROOT) | external |
| `sniffer_stop` (ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ) | — (terminate → 5 c → kill; pid из data/sniffer.pid) | external |
| `sniffer_restart` (ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ) | — (stop + start с последним конфигом) | external |
| `sniffer_stats` | GET /api/stats (пакеты, байты, per_proto, top_types, top_clients) | read |
| `sniffer_series` | GET /api/series (точки = секунды: pps/bps) | read |
| `sniffer_sessions` | GET /api/sessions | read |
| `sniffer_packets` (фильтры на стороне MCP) | GET /api/packets?limit=N + клиентская фильтрация search/protocol/direction/min_size/max_size | read |
| `sniffer_packet_detail` | GET /api/packet/<id> (полный разбор + hexdump) | read |
| `sniffer_alerts` | GET /api/alerts | read |
| `sniffer_config_get` | GET /api/config (+parsers) | external |
| `sniffer_reload` | POST /api/reload (горячая перезагрузка правил тревог) | external |
| `sniffer_export_csv` | GET /api/export/csv → список файлов | external |
| `sniffer_list_capture` | — (os.listdir capture/ с размерами/mtimes) | read |
| `sniffer_read_capture` | — (чтение хвоста текстового файла capture/, см. «Безопасность») | read |
| `sniffer_analyze` | GET /api/packets?limit=2000 + /api/alerts → агрегат: протоколы, клиенты, ошибки/исключения, гигантские пакеты, crit-тревоги | read |

Переменные окружения: `SNIFFER_PANEL_URL` (адрес панели, по умолчанию
`http://127.0.0.1:9500`; задаётся и через `config/settings.yaml` →
`mcp_servers.sniffer.env` со стилем `${SNIFFER_PANEL_URL}`), `SNIFFER_WEB_PORT`
(порт панели, если без URL), `PROJECT_ROOT` (корень проекта для subprocess),
`USNIFF_ROOT` (каталог данных сниффера).

## Запуск

```bash
# из корня проекта llm-agent

# самопроверка (эхо-сервер + прокси in-process, без внешней сети):
python -m src.sniffer.selftest          # печатает PASS/FAIL

# вручную, сценарий АЗС (старые порты слушает сниффер, RemoteServer уехал на +1):
python -m src.sniffer --ports 3003:3004,10010:10011

# свой конфиг:
python -m src.sniffer --config src/sniffer/config.json
python -m src.sniffer --check-config

# веб-панель: http://127.0.0.1:9500/  (SSE-поток: /api/stream)
```

Через агента: в чате — «проверь трафик на порту 3003» / «покажи тревоги
сниффера» / «запусти сниффер на портах 3003:3004». Роутер выберет агента
`sniffer`; запуск/остановка пойдут через approval gate.

### Сценарий АЗС (из README UniversalSniffer)

```
1. Остановите RemoteServer.
2. Запустите RemoteServer на портах-преемниках: 3004 и 10011.
3. Сниффер слушает старые порты (3003, 10010) и перенаправляет трафик
   на новые: python -m src.sniffer --ports 3003:3004,10010:10011
4. АЗС продолжают подключаться к старым портам как обычно; весь трафик
   виден в консоли, на веб-панели и в capture/ (JSONL/SQLite/PCAP).
5. Тревоги: «Откат безналичной оплаты» (CASHLESS_ROLLBACK ≥2 за 60 c),
   «Thrift EXCEPTION», «Гигантский пакет», «Ошибка БД в запросе».
```

## Безопасность

- **Approval gate**: `sniffer_start/stop/restart` имеют `danger: external` и
  перечислены в `dangerous_tools` агента → модальное окно подтверждения;
  в промпте агента закреплено «сначала status, старт — только после «да»».
- **Path traversal**: `sniffer_read_capture` принимает только имя файла
  (без слэшей и `..`), расширение ограничено белым списком
  (`.jsonl .log .csv .txt` — текст; `.db .pcap .sqlite` — только метаданные
  и размер, содержимое не читается).
- **Таймауты**: все HTTP-вызовы к панели — 5 c; недоступная панель → JSON с
  `error` и подсказкой, а не исключение.
- Сниффер слушает только указанные порты; панель по умолчанию на 127.0.0.1/9500
  (конфигом можно сменить хост/порт).

## Плагины парсеров

Парсер — обычный `.py` в `data/sniffer/plugins/` (загружается при старте,
может переопределить встроенный по NAME):

```python
NAME = "my_protocol"        # protocol в логах
LABEL = "Мой протокол"
PRIORITY = 50               # приоритет автодетекта (0..100)

def detect(chunk, ctx) -> int:        # 0..100 — уверенность
    return 0

def create_framer(ctx):               # None => режим «кусками»
    return None

def parse(frame, ctx) -> dict:        # поля анализа (см. hub.SUMMARY_KEYS)
    return {"summary": "…"}
```

## Troubleshooting

| Симптом | Причина / действие |
|---|---|
| `sniffer_status` → «stopped» | Сниффер не запущен — `sniffer_start` (после подтверждения) или `python -m src.sniffer --ports …` вручную |
| Процесс есть, панель не отвечает (`running/unreachable`) | Панель ещё поднимается — повторите `sniffer_status`; проверьте порт `SNIFFER_PANEL_URL`/`web.port` |
| «процесс завершился сразу после старта» | Порт занят или плохой конфиг — смотри `data/sniffer-console.log` или `--check-config` |
| Пакеты не появляются | Трафик идёт мимо сниффера: проверьте пары портов (клиент должен подключаться к listen-порту) и `sniffer_sessions` |
| `sniffer_list_capture` → каталог не найден | Файлы захвата появляются после первого запуска; ищутся в `data/sniffer/capture`, `src/sniffer/capture`, `capture` |
| Порт 9500 занят | `--web-port 8080` при запуске или `SNIFFER_PANEL_URL=http://127.0.0.1:8080` |
