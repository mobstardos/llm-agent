"""Подсчёт токенов."""
from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _tiktoken_enc():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception as e:
        logger.warning("tiktoken недоступен: %s", e)
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _tiktoken_enc()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def count_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += count_tokens(m.get("content", "") or "")
        for tc in (m.get("tool_calls") or []):
            args = tc.get("function", {}).get("arguments", "")
            total += count_tokens(args)
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if count_tokens(text) <= max_tokens:
        return text
    enc = _tiktoken_enc()
    if enc is not None:
        try:
            ids = enc.encode(text)[:max_tokens]
            return enc.decode(ids) + "\n...[обрезано]"
        except Exception:
            pass
    return text[: max_tokens * 4] + "\n...[обрезано]"
