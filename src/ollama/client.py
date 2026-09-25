"""Асинхронный клиент Ollama.

Малая модель (1.5B) целиком помещается в 2GB VRAM.
GPU используется автоматически через Ollama.

Модели:
- qwen2.5:1.5b-instruct — универсал, ~1.0 GB, быстро
- deepseek-r1-distill-qwen:1.5b — reasoning, ~1.1 GB, точнее
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ollama: "OllamaClient | None" = None


class OllamaClient:
    """Обёртка над Ollama HTTP API."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        keep_alive: str = "5m",
    ):
        self.base_url = (
            base_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
        ).rstrip("/")
        self.model = model or os.getenv(
            "OLLAMA_MODEL", "qwen2.5:1.5b-instruct",
        )
        self.timeout = timeout
        self.keep_alive = keep_alive

    # ═══════════════════════════════════════════════════════
    # Health / info
    # ═══════════════════════════════════════════════════════
    async def health(self) -> bool:
        """Проверить, отвечает ли Ollama."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Список установленных моделей."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                return [m["name"] for m in r.json().get("models", [])]
        except Exception as e:
            logger.warning("Ollama list_models: %s", e)
            return []

    async def model_info(self, model: str | None = None) -> dict:
        """Информация о модели (размер, параметры)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{self.base_url}/api/show",
                    json={"name": model or self.model},
                )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.warning("Ollama model_info: %s", e)
            return {}

    # ═══════════════════════════════════════════════════════
    # Generate (single-turn)
    # ═══════════════════════════════════════════════════════
    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        format_json: bool = False,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> str:
        """Генерация текста."""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
            },
        }
        if system:
            payload["system"] = system
        if format_json:
            payload["format"] = "json"
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                return data.get("response", "") or ""
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Ollama HTTP %s: %s",
                e.response.status_code, e.response.text[:300],
            )
            return ""
        except Exception as e:
            logger.warning("Ollama generate: %s", e)
            return ""

    async def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
    ) -> dict | None:
        """Генерация с парсингом JSON.

        Использует format=json в Ollama API — гарантирует валидный JSON.
        """
        text = await self.generate(
            prompt,
            system=system,
            format_json=True,
            temperature=temperature,
        )
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse: %s, text=%s", e, text[:200])
            # Иногда модель добавляет markdown-обёртку
            cleaned = text.strip().strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                return json.loads(cleaned)
            except Exception:
                return None

    # ═══════════════════════════════════════════════════════
    # Chat (multi-turn)
    # ═══════════════════════════════════════════════════════
    async def chat(
        self,
        messages: list[dict],
        *,
        format_json: bool = False,
        temperature: float = 0.1,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Чат (multi-turn)."""
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if format_json:
            payload["format"] = "json"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                return data.get("message", {}).get("content", "") or ""
        except Exception as e:
            logger.warning("Ollama chat: %s", e)
            return ""

    # ═══════════════════════════════════════════════════════
    # Модели: GPU info
    # ═══════════════════════════════════════════════════════
    async def running_models(self) -> list[dict]:
        """Загруженные в память модели."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self.base_url}/api/ps")
                r.raise_for_status()
                return r.json().get("models", [])
        except Exception:
            return []

    async def unload_model(self, model: str | None = None) -> bool:
        """Выгрузить модель из памяти (keep_alive=0)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": model or self.model,
                        "keep_alive": "0s",
                    },
                )
                return r.status_code == 200
        except Exception:
            return False


def get_ollama() -> OllamaClient:
    """Глобальный singleton клиента."""
    global _ollama
    if _ollama is None:
        _ollama = OllamaClient()
    return _ollama
