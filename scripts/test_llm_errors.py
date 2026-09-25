#!/usr/bin/env python3
"""Тесты src/llm_errors.py + интеграции с оркестратором.

Запуск: python scripts/test_llm_errors.py
Только стандартная библиотека — openai-клиент не нужен (интеграционная часть
запустится, если зависимости установлены).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.llm_errors import describe, friendly_llm_error  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}: {detail}")


# ── 1. describe(): точная ошибка пользователя (OpenAI 403 region) ─────
region_body = (
    "Error code: 403 - {'error': {'code': 'unsupported_country_region_territory', "
    "'message': 'Country, region, or territory not supported', 'param': None, "
    "'type': 'request_forbidden'}}"
)
h = describe(403, region_body)
check("403 region → подсказка про регион",
      "регион" in h and "api.openai.com" in h and "--reconfigure-ai" in h, h)

h = describe(401, "{'error': {'code': 'invalid_api_key', 'message': 'Incorrect API key'}}")
check("401 invalid_api_key → подсказка про ключ", "LLM_API_KEY" in h, h)

h = describe(404, "{'error': {'code': 'model_not_found', 'message': 'The model `qwen3.8-max` does not exist'}}")
check("404 model_not_found → подсказка про модель",
      "модель" in h and "models.yaml" in h, h)

h = describe(429, "{'error': {'code': 'insufficient_quota', 'message': 'You exceeded your current quota'}}")
check("429 insufficient_quota → подсказка про баланс", "баланс" in h or "квота" in h, h)

h = describe(429, "{'error': {'code': 'rate_limit_exceeded'}}")
check("429 rate_limit → подсказка про лимит", "лимит" in h, h)

h = describe(None, "Connection error.")
check("Connection error → подсказка про запуск провайдера",
      "недоступен" in h and "qwenproxy" in h, h)

h = describe(None, "Request timed out.")
check("timeout → подсказка про таймаут", "таймаут" in h, h)

h = describe(503, "service unavailable")
check("503 → подсказка про провайдера", "503" in h, h)

h = describe(418, "teapot")
check("неизвестный статус → пустая подсказка", h == "", h)


# ── 2. friendly_llm_error(): имитация openai.APIStatusError ────────────
class FakeAPIStatusError(Exception):
    def __init__(self, status_code: int, body: dict, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


exc = FakeAPIStatusError(
    403,
    {"error": {"code": "unsupported_country_region_territory",
               "message": "Country, region, or territory not supported",
               "param": None, "type": "request_forbidden"}},
    "Error code: 403 - {'error': {'code': 'unsupported_country_region_territory', ...}}",
)
h = friendly_llm_error(exc)
check("friendly_llm_error(openai 403 region)", "регион" in h, h)

exc2 = FakeAPIStatusError(
    401, {"error": {"message": "Incorrect API key provided"}},
    "Error code: 401 - Incorrect API key provided",
)
h = friendly_llm_error(exc2)
check("friendly_llm_error(openai 401)", "ключ" in h, h)

h = friendly_llm_error(ConnectionError("Connection error."))
check("friendly_llm_error(ConnectionError)", "недоступен" in h, h)

h = friendly_llm_error(RuntimeError("что-то совсем неизвестное"))
check("неизвестное исключение → пусто", h == "", h)


# ── 3. Интеграция с оркестратором (импорт полного стека) ──────────────
async def orchestrator_integration() -> None:
    try:
        from src.orchestrator import Orchestrator

        class FakeLLM:
            async def chat(self, *a, **kw):
                raise FakeAPIStatusError(
                    403,
                    {"error": {"code": "unsupported_country_region_territory",
                               "message": "Country, region, or territory not supported",
                               "param": None, "type": "request_forbidden"}},
                    "Error code: 403 - {'error': {'code': 'unsupported_country_region_territory', ...}}",
                )

        class FakeRuntime:
            agents = {"file": object()}
            def __len__(self):
                return 1

        orch = Orchestrator(FakeLLM(), FakeRuntime())
        result = await orch.route("проверка связи")
        reason = result.get("reason", "")
        check("orchestrator.route отдаёт дружелюбную подсказку",
              result["agents"] == [] and "регион" in reason and "Ошибка роутинга" in reason,
              reason)
    except ImportError as e:
        print(f"  ! интеграционная часть пропущена (нет зависимостей): {e}")


print("\n─ Интеграция с оркестратором ─")
asyncio.run(orchestrator_integration())

print(f"\n{'='*50}\nИТОГО: ✓ {PASS} / ✗ {FAIL}")
sys.exit(1 if FAIL else 0)
