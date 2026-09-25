"""Человекочитаемая расшифровка ошибок LLM-провайдеров.

Только стандартная библиотека: модуль используется оркестратором (src/orchestrator.py)
и мастером первого запуска (first_run.py) ДО установки зависимостей.

Задача: вместо сырого «Error code: 403 - {'error': {...}}» в чате и консоли
показывать понятную русскую подсказку с конкретным способом решения.
"""
from __future__ import annotations

import json
import re

# ── Правила: (ключевые слова в теле ошибки в нижнем регистре) → подсказка ──
# Порядок важен: сначала специфичные, потом общие.
_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("deepseek http 401", "qwen http 401", "deepseek http 403",
         "qwen http 403", "нет кук deepseek", "нет кук qwen",
         "куки/токен отклонены"),
        "веб-чат по кукам не авторизован: куки протухли или ещё не собраны. "
        "Обновите в один клик: расширение Bridge → 🔑 DeepSeek / 🔑 Qwen → "
        "войдите в чат (chat.deepseek.com / chat.qwen.ai). Статус: вкладка "
        "«Мост» или GET /api/bridge/cookies/status. Перезапуск сервера "
        "не нужен — модели deepseek_web / qwen_web подхватятся сразу",
    ),
    (
        ("unsupported_country_region_territory",
         "country, region, or territory not supported"),
        "провайдер блокирует ваш регион — OpenAI не обслуживает РФ/РБ "
        "(запрос ушёл на api.openai.com). Решение: python first_run.py "
        "--reconfigure-ai → выберите DeepSeek API / Ollama (локально) / "
        "qwenproxy (браузерный режим); проверка связи: python first_run.py --check-llm",
    ),
    (
        ("invalid_api_key", "invalid request: initial text after context",
         "incorrect api key", "invalid x-api-key", "api key not valid"),
        "провайдер отклонил API-ключ (неверный/опечатка/отозван). "
        "Проверьте LLM_API_KEY в .env или перенастройте: python first_run.py --reconfigure-ai",
    ),
    (
        ("insufficient_quota", "exceeded your current quota",
         "insufficient_user_quota", "billing", "balance is empty",
         "not enough money", "arbalance"),
        "у провайдера исчерпана квота/баланс. Пополните аккаунт у провайдера "
        "или смените его: python first_run.py --reconfigure-ai",
    ),
    (
        ("model_not_found", "model_does_not_exist", "does not exist",
         "no such model", "unknown model", "model not found",
         "not found model", "invalid model"),
        "провайдер не знает такую модель (проверьте имя): модель из "
        "выпадающего списка должна совпадать с моделями провайдера — "
        "см. LLM_MODEL в .env и config/models.yaml",
    ),
    (
        ("rate_limit_exceeded", "rate limit", "too many requests", "ratelimit"),
        "сработал лимит запросов (429) — подождите немного и повторите",
    ),
    (
        ("permission denied", "access denied", "forbidden", "not_allowed"),
        "доступ запрещён провайдером (403): проверьте права ключа "
        "и доступность сервиса из вашего региона",
    ),
]

# Статус-коды без ключевых слов → общие подсказки
_STATUS_HINTS: dict[int, str] = {
    401: "ошибка авторизации (401): ключ не принят — проверьте LLM_API_KEY в .env",
    403: "доступ запрещён (403): провайдер ограничил доступ (регион/права ключа)",
    404: "не найдено (404): проверьте LLM_BASE_URL (путь /v1) и имя модели",
    429: "лимит запросов (429): пауза/квота/баланс — уточните у провайдера",
    500: "внутренняя ошибка провайдера (500) — повторите позже",
    502: "провайдер недоступен (502) — повторите позже",
    503: "провайдер перегружен (503) — повторите позже",
}

_CONN_KEYS = (
    "connection error", "connection refused", "connect error", "econnrefused",
    "connectionreseterror", "connection aborted", "remote end closed",
    "nodename nor servname", "name or service not known", "getaddrinfo",
    "failed to establish", "network is unreachable", "err_connection",
)


def describe(status: int | None, body: str) -> str:
    """Подсказка по статус-коду и тексту ответа. Пустая строка — нет правила."""
    text = (body or "").lower()

    # Специфичные ключевые слова (включая те, что зашиты в JSON провайдера)
    for keys, hint in _RULES:
        for k in keys:
            if k in text:
                return hint

    # Сетевые проблемы (нет статус-кода — это соединение)
    for k in _CONN_KEYS:
        if k in text:
            return (
                "провайдер недоступен (Connection error): сервис не запущен "
                "или неверный LLM_BASE_URL. Проверка связи: "
                "python first_run.py --check-llm. Для qwenproxy: "
                "npm i -g qwenproxy-cli → запустите qpx → вкладка [5] "
                "Accounts → A (email и пароль от chat.qwen.ai) → "
                "перезапуск run.py"
            )
    if "timed out" in text or "timeout" in text or "deadline" in text:
        return (
            "таймаут провайдера: ответ идёт слишком долго или сеть недоступна — "
            "повторите запрос или проверьте LLM_BASE_URL"
        )

    # Общие статус-коды
    if status is not None:
        return _STATUS_HINTS.get(int(status), "")
    return ""


def friendly_llm_error(exc: BaseException) -> str:
    """Извлекает статус/тело из исключения (openai.* или urllib) и даёт подсказку."""
    status = getattr(exc, "status_code", None)
    body = str(exc)

    extra = getattr(exc, "body", None)
    if extra and not isinstance(extra, str):
        try:
            body += " " + json.dumps(extra, ensure_ascii=False)
        except Exception:  # noqa: BLE001 — подсказка не должна падать
            pass

    if status is None:
        m = re.search(r"Error code: (\d{3})", body)
        if m:
            status = int(m.group(1))

    try:
        return describe(int(status) if status else None, body)
    except Exception:  # noqa: BLE001
        return ""
