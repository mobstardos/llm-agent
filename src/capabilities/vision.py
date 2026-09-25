"""Vision providers: Qwen-VL через OpenAI API, LLaVA через Ollama."""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _image_data_url(path: str | Path) -> str:
    """Кодирует изображение в data URL."""
    p = Path(path)
    suffix = p.suffix.lower().lstrip(".")
    if suffix == "jpg":
        suffix = "jpeg"
    mime = f"image/{suffix or 'png'}"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


class QwenVLProvider:
    """Vision через Qwen-VL (OpenAI-совместимый API).

    Работает как с qwenproxy-cli, так и с любым OpenAI-совместимым
    сервером, поддерживающим image_url в messages.
    """

    def __init__(
        self, base_url: str, api_key: str, model: str,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def describe(
        self, image_path: str | Path, question: str = "",
    ) -> str:
        question = question or "Опиши это изображение подробно."
        return await self._chat(image_path, question)

    async def ocr(
        self, image_path: str | Path, lang: str = "rus+eng",
    ) -> str:
        prompt = (
            f"Извлеки весь текст с этого изображения (языки: {lang}). "
            "Верни только текст, без комментариев."
        )
        return await self._chat(image_path, prompt)

    async def classify(
        self, image_path: str | Path, classes: list[str],
    ) -> str:
        prompt = (
            "Классифицируй это изображение. Возможные классы: "
            f"{', '.join(classes)}. Ответь одним словом — именем класса."
        )
        return await self._chat(image_path, prompt)

    async def compare(
        self, image_a: str | Path, image_b: str | Path,
    ) -> str:
        prompt = (
            "Сравни два изображения. Что изменилось? Опиши различия."
        )
        url_a = _image_data_url(image_a)
        url_b = _image_data_url(image_b)

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url_a}},
                    {"type": "image_url", "image_url": {"url": url_b}},
                ],
            },
        ]
        return await self._chat_messages(messages)

    async def _chat(self, image_path: str | Path, prompt: str) -> str:
        url = _image_data_url(image_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ]
        return await self._chat_messages(messages)

    async def _chat_messages(self, messages: list[dict]) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.1,
                    },
                )
                r.raise_for_status()
                data = r.json()
                return (
                    data["choices"][0]["message"].get("content") or ""
                ).strip()
        except httpx.HTTPStatusError as e:
            logger.warning("Qwen-VL HTTP %s: %s", e.response.status_code,
                           e.response.text[:300])
            raise RuntimeError(
                f"Qwen-VL HTTP {e.response.status_code}: "
                f"{e.response.text[:200]}"
            )
        except Exception as e:
            logger.exception("Qwen-VL failed")
            raise RuntimeError(f"Qwen-VL: {e}")


class OllamaLlavaProvider:
    """Vision через Ollama (LLaVA)."""

    def __init__(
        self, base_url: str, model: str, timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def describe(
        self, image_path: str | Path, question: str = "",
    ) -> str:
        question = question or "Опиши это изображение."
        return await self._chat(image_path, question)

    async def _chat(self, image_path: str | Path, prompt: str) -> str:
        img_b64 = base64.b64encode(
            Path(image_path).read_bytes()
        ).decode("ascii")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "images": [img_b64],
                        "stream": False,
                    },
                )
                r.raise_for_status()
                data = r.json()
                return (data.get("response") or "").strip()
        except Exception as e:
            logger.exception("Ollama LLaVA failed")
            raise RuntimeError(f"Ollama: {e}")


class TesseractProvider:
    """OCR через Tesseract."""

    def __init__(self, lang: str = "rus+eng"):
        self.lang = lang

    async def ocr(
        self, image_path: str | Path, lang: str | None = None,
    ) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, self._ocr_sync, str(image_path), lang or self.lang,
        )

    @staticmethod
    def _ocr_sync(image_path: str, lang: str) -> str:
        try:
            from PIL import Image
            import pytesseract
            with Image.open(image_path) as img:
                return pytesseract.image_to_string(img, lang=lang)
        except ImportError as e:
            raise RuntimeError(f"tesseract/Pillow недоступны: {e}")
        except Exception as e:
            raise RuntimeError(f"Tesseract: {e}")


class VisionProviderFactory:
    """Фабрика провайдеров по capability resolve."""

    @staticmethod
    def create_from_resolved(
        capability_yaml: dict, resolved: dict,
    ) -> Any | None:
        """Создаёт основной провайдер из resolved capability."""
        primary_id = resolved.get("primary")
        if not primary_id:
            return None

        providers = capability_yaml.get("providers", [])
        for p in providers:
            if p.get("id") != primary_id:
                continue
            return VisionProviderFactory._build(p)
        return None

    @staticmethod
    def _build(provider_cfg: dict) -> Any | None:
        ptype = provider_cfg.get("type")
        if ptype == "openai_compatible":
            return QwenVLProvider(
                base_url=provider_cfg.get("base_url", ""),
                api_key=provider_cfg.get("api_key", ""),
                model=provider_cfg.get("model", "qwen-vl-max"),
            )
        if ptype == "ollama":
            return OllamaLlavaProvider(
                base_url=provider_cfg.get("base_url", ""),
                model=provider_cfg.get("model", "llava:13b"),
            )
        if ptype == "local" and provider_cfg.get("binary") == "tesseract":
            return TesseractProvider()
        return None
