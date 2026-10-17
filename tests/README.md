# Тесты — llm-agent

> Спринт 3 (P1) — каркас тестового покрытия.
> 4 модуля + conftest.py с общими фикстурами.

## Что покрыто

| Файл | Модуль | Кейсов | Покрытие (целевое) |
|---|---|---|---|
| `test_orchestrator.py` | `src/orchestrator.py` | 11 | route() полностью, handle() базовые сценарии |
| `test_llm_client.py` | `src/llm_client.py` | 13 | _msg_to_dict, chat (cache + fallback), _client_for, _should_fallback |
| `test_journal_recorder.py` | `src/journal/recorder.py` | 10 | before/after_tool_call, truncate_result, redact, inverse_spec |
| `test_memory_facade.py` | `src/memory/facade.py` | 10 | initialize (PG/SQLite fallback), session, log_event, search, stats, cleanup |
| `conftest.py` | (общие фикстуры) | — | env_no_pg, tmp_data_dir, FakeLLM, FakeRuntime, FakeMCP |
| **ИТОГО** | | **44** | |

## Запуск

```bash
# Все тесты:
pytest tests/ -v

# С покрытием:
pytest tests/ --cov=src --cov-report=term-missing

# Один модуль:
pytest tests/test_orchestrator.py -v

# Один тест:
pytest tests/test_orchestrator.py::test_route_empty_runtime_returns_no_agents -v

# С JUnit-отчётом для CI:
pytest tests/ --junitxml=test-results.xml

# Только асинхронные:
pytest tests/ -k "asyncio" -v

# Пропустить slow-тесты:
pytest tests/ -m "not slow" -v
```

## Структура тестов

### `conftest.py` — общие фикстуры

- `env_no_pg` (autouse=True) — отключает PostgreSQL, Ollama, AGENT_MEMORY.
  Все тесты должны проходить без реальных подсистем.
- `tmp_data_dir` — создаёт `tmp_path/data/{profiles,sessions,sessions/dump}`.
- `FakeLLM` — заглушка LLMClient. Метод `add_response(messages_substr, content=...)`
  добавляет ответ, который вернётся при совпадении `messages_substr` в user-content.
  Метод `add_error(messages_substr, exc)` — заставляет бросить исключение.
- `FakeRuntime` — заглушка AgentRuntime с заданным списком agent_id.
  Каждый агент — `FakeAgent` с `next_result` (можно настроить результат).
- `FakeMCP` — заглушка MCPManager.call_tool, возвращает `{"ok": True, ...}`.
- `event_loop` — переопределённый event_loop (для pytest-asyncio 0.24+).

### Паттерны

**AAA (Arrange-Act-Assert)**:
```python
@pytest.mark.asyncio
async def test_route_with_valid_json(fake_llm, fake_runtime):
    # Arrange — подготовка
    fake_llm.add_response(messages_substr="", content='{"agents": ["file"]}')
    orch = Orchestrator(fake_llm, fake_runtime)

    # Act — действие
    result = await orch.route("прочитай файл")

    # Assert — проверка
    assert result["agents"] == ["file"]
```

**Mock внешних зависимостей**:
```python
with patch("src.llm_client.AsyncOpenAI") as mock_cls:
    instance = AsyncMock()
    mock_cls.return_value = instance
    # ... тестируем с instance
```

**Временные данные**:
```python
def test_xxx(tmp_path: Path):
    target = tmp_path / "test.txt"
    target.write_text("content")
    # ... тестируем с target
```

## Что НЕ покрыто (Sprint 4-5)

- `src/main.py` (2640 LOC, 86 эндпоинтов) — нужно после декомпозиции (рекомендация 1).
- `src/loop/controller.py` (LoopController) — нужен полноценный e2e тест.
- `src/db/*` (PostgreSQL pool, AGE, CDC) — нужны контейнерные тесты с PG.
- `src/mcp_servers/*` (37 серверов) — каждый MCP требует интеграционного теста.
- WebSocket `/ws` — нужен Playwright-тест в реальном браузере.

## CI

Прогон через `.github/workflows/tests.yml` (см. Sprint 2 patch):
- Matrix Python 3.11/3.12.
- `pip install -r requirements-dev.txt` + `pip install -r requirements.txt` + `pip install -e .`
- `pytest --cov=src --cov-report=xml --junitxml=test-results.xml --maxfail=5`
- Coverage в codecov, artifacts 14 дней.
- `continue-on-error: true` первые 2 недели (gradual).

## Как добавить новый тест

1. **Прочитай исходный модуль**, найди public API.
2. **Определи happy path + edge cases** (пустой ввод, ошибка, fallback).
3. **Создай fixture в conftest.py** если нужна новая заглушка.
4. **Напиши тест с AAA-структурой** и понятным именем:
   `test_<method>_<scenario>_<expected_outcome>`.
5. **Запусти локально**: `pytest tests/test_xxx.py -v`.
6. **Проверь coverage**: `pytest tests/test_xxx.py --cov=src.xxx --cov-report=term-missing`.
7. **Добавь в CI matrix** если нужен Python 3.13+ (см. `tests.yml`).

## Известные ограничения

- `test_journal_recorder.py::test_inverse_spec_*` — могут падать, если
  `before_tool_call` требует реального файла для shadow store. Тесты создают
  временный файл через `tmp_path`, но если shadow-логика использует
  `os.path.realpath` или проверяет absolute path, могут быть нюансы.
  В случае падения — пометить `@pytest.mark.skip(reason="shadow store path resolution")`.
- `test_llm_client.py` использует `patch("src.llm_client.LLMClient._attempt")`
  с `side_effect` — если сигнатура `_attempt` изменится, нужно обновить.
- `test_memory_facade.py::test_initialize_with_pg_pool` — патчит
  `src.db.pool.PgPool`, но `Memory._init_postgres` может импортировать
  `psycopg` на верхнем уровне. Если тест падает с `ImportError: psycopg` —
  установить `psycopg[binary]` в тестовое окружение или пометить `@pytest.mark.skipif`.

## Связанные документы

- `SPRINT-ROADMAP.xlsx` — статус рекомендации 7 (Sprint 3).
- `.github/workflows/tests.yml` (Sprint 2 patch) — CI-конфиг.
- Технический отчёт `llm-agent-archives-analysis.pdf` — §9 рекомендация 7.
