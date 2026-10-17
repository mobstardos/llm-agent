"""MCP-сервер: изображения.

Vision — через capability resolver.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("image-mcp")

_vision_provider = None
_ocr_provider = None
_resolved = None


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


async def _load_providers():
    """Резолвит vision и ocr через CapabilityResolver."""
    global _vision_provider, _ocr_provider, _resolved

    if _vision_provider is not None or _ocr_provider is not None:
        return

    try:
        from src.core.loader import DeclarationLoader
        from src.core.runtime_config import RuntimeConfig
        from src.core.capabilities import CapabilityResolver
        from src.capabilities.vision import VisionProviderFactory

        loader = DeclarationLoader()
        capabilities = loader.load_capabilities()

        runtime = RuntimeConfig()
        resolver = CapabilityResolver(runtime)

        resolved = {}
        for cid, cap in capabilities.items():
            resolved[cid] = await resolver.resolve_one(cap)
        _resolved = resolved

        # Vision
        vision_cap = capabilities.get("vision")
        vision_resolved = resolved.get("vision", {})
        if vision_cap and vision_resolved.get("primary"):
            cap_dict = {
                "providers": [
                    {
                        "id": p.id, "type": p.type,
                        "base_url": p.base_url, "api_key": p.api_key,
                        "model": p.model, "binary": p.binary,
                    }
                    for p in vision_cap.providers
                ],
            }
            _vision_provider = VisionProviderFactory.create_from_resolved(
                cap_dict, vision_resolved,
            )

        # OCR
        ocr_cap = capabilities.get("ocr")
        ocr_resolved = resolved.get("ocr", {})
        if ocr_cap and ocr_resolved.get("primary"):
            cap_dict = {
                "providers": [
                    {
                        "id": p.id, "type": p.type,
                        "base_url": p.base_url, "api_key": p.api_key,
                        "model": p.model, "binary": p.binary,
                    }
                    for p in ocr_cap.providers
                ],
            }
            _ocr_provider = VisionProviderFactory.create_from_resolved(
                cap_dict, ocr_resolved,
            )
            if _ocr_provider is None:
                from src.capabilities.vision import TesseractProvider
                _ocr_provider = TesseractProvider()

    except Exception as e:
        logger.exception("Ошибка загрузки providers: %s", e)


app = Server("image")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="describe",
             description="Описать содержимое изображения (Vision).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "question": {"type": "string"},
                 },
                 "required": ["path"],
             }),
        Tool(name="ocr",
             description="Извлечь текст с изображения.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "lang": {"type": "string", "default": "rus+eng"},
                 },
                 "required": ["path"],
             }),
        Tool(name="classify",
             description="Классифицировать изображение по списку классов.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "classes": {"type": "array",
                                 "items": {"type": "string"}},
                 },
                 "required": ["path", "classes"],
             }),
        Tool(name="compare",
             description="Сравнить два изображения (Vision).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path_a": {"type": "string"},
                     "path_b": {"type": "string"},
                 },
                 "required": ["path_a", "path_b"],
             }),
        Tool(name="metadata",
             description="Метаданные изображения.",
             inputSchema={
                 "type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"],
             }),
        Tool(name="resize",
             description="Изменить размер.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "width": {"type": "integer"},
                     "height": {"type": "integer"},
                     "output": {"type": "string"},
                 },
                 "required": ["path", "width", "height"],
             }),
        Tool(name="crop",
             description="Обрезать.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "left": {"type": "integer"},
                     "top": {"type": "integer"},
                     "right": {"type": "integer"},
                     "bottom": {"type": "integer"},
                     "output": {"type": "string"},
                 },
                 "required": ["path", "left", "top", "right", "bottom"],
             }),
        Tool(name="convert",
             description="Конвертировать в другой формат.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "format": {"type": "string"},
                     "output": {"type": "string"},
                 },
                 "required": ["path", "format"],
             }),
        Tool(name="optimize",
             description="Сжать изображение.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "quality": {"type": "integer", "default": 85},
                     "output": {"type": "string"},
                 },
                 "required": ["path"],
             }),
        Tool(name="generate_favicon_set",
             description="Набор favicon (16,32,48,64,128,256).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "output_dir": {"type": "string", "default": "favicons"},
                 },
                 "required": ["path"],
             }),
        Tool(name="diff_visual",
             description="Визуальная разница между двумя изображениями (SSIM).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path_a": {"type": "string"},
                     "path_b": {"type": "string"},
                 },
                 "required": ["path_a", "path_b"],
             }),
        Tool(name="vision_info",
             description="Информация о доступных vision/ocr провайдерах.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        await _load_providers()

        if name == "vision_info":
            return [TextContent(
                type="text",
                text=json.dumps({
                    "vision_provider": type(_vision_provider).__name__
                        if _vision_provider else None,
                    "ocr_provider": type(_ocr_provider).__name__
                        if _ocr_provider else None,
                    "resolved": _resolved or {},
                }, ensure_ascii=False, indent=2),
            )]

        p = _safe(arguments["path"])
        if not p.exists():
            return [TextContent(type="text", text=f"Файл не найден: {p}")]

        # ─── Vision-функции ─────────────────────
        if name == "describe":
            if _vision_provider is None:
                return [TextContent(
                    type="text", text="Vision-провайдер недоступен",
                )]
            text = await _vision_provider.describe(
                p, arguments.get("question", ""),
            )
            return [TextContent(type="text", text=text)]

        if name == "classify":
            if _vision_provider is None:
                return [TextContent(
                    type="text", text="Vision-провайдер недоступен",
                )]
            if not hasattr(_vision_provider, "classify"):
                return [TextContent(
                    type="text",
                    text="Текущий провайдер не поддерживает classify",
                )]
            text = await _vision_provider.classify(
                p, arguments.get("classes", []),
            )
            return [TextContent(type="text", text=text)]

        if name == "compare":
            pa = _safe(arguments["path_a"])
            pb = _safe(arguments["path_b"])
            if _vision_provider is None:
                return [TextContent(
                    type="text", text="Vision-провайдер недоступен",
                )]
            if not hasattr(_vision_provider, "compare"):
                return [TextContent(
                    type="text",
                    text="Текущий провайдер не поддерживает compare",
                )]
            text = await _vision_provider.compare(pa, pb)
            return [TextContent(type="text", text=text)]

        if name == "ocr":
            if _ocr_provider is None:
                return [TextContent(
                    type="text", text="OCR-провайдер недоступен",
                )]
            lang = arguments.get("lang", "rus+eng")
            if hasattr(_ocr_provider, "ocr"):
                text = await _ocr_provider.ocr(p, lang)
                return [TextContent(type="text", text=text or "(пусто)")]
            return [TextContent(
                type="text", text="Провайдер не поддерживает ocr",
            )]

        # ─── Метаданные ─────────────────────────
        if name == "metadata":
            return [TextContent(
                type="text",
                text=json.dumps(_metadata(p), ensure_ascii=False, indent=2),
            )]

        # ─── Обработка ──────────────────────────
        if name == "resize":
            out = _output_path(
                p, arguments.get("output"),
            )
            _resize(p, out, arguments["width"], arguments["height"])
            return [TextContent(type="text", text=f"Сохранено: {out}")]

        if name == "crop":
            out = _output_path(p, arguments.get("output"))
            _crop(
                p, out,
                arguments["left"], arguments["top"],
                arguments["right"], arguments["bottom"],
            )
            return [TextContent(type="text", text=f"Сохранено: {out}")]

        if name == "convert":
            fmt = arguments["format"].lstrip(".").lower()
            out = arguments.get("output") or str(
                p.with_suffix(f".{fmt}")
            )
            out_path = _safe(out)
            _convert(p, out_path, fmt)
            return [TextContent(type="text", text=f"Сохранено: {out_path}")]

        if name == "optimize":
            out = _output_path(p, arguments.get("output"))
            q = arguments.get("quality", 85)
            _optimize(p, out, q)
            return [TextContent(type="text", text=f"Сохранено: {out} (q={q})")]

        if name == "generate_favicon_set":
            out_dir = _safe(arguments.get("output_dir", "favicons"))
            out_dir.mkdir(parents=True, exist_ok=True)
            paths = _favicon_set(p, out_dir)
            return [TextContent(
                type="text",
                text=json.dumps(paths, ensure_ascii=False, indent=2),
            )]

        if name == "diff_visual":
            pa = _safe(arguments["path_a"])
            pb = _safe(arguments["path_b"])
            result = _visual_diff(pa, pb)
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Image tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


# ═════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════
def _output_path(p: Path, output: str | None) -> Path:
    if output:
        return _safe(output)
    stem = p.stem
    return p.parent / f"{stem}_out{p.suffix}"


def _metadata(p: Path) -> dict:
    from PIL import Image
    with Image.open(p) as img:
        meta = {
            "width": img.width,
            "height": img.height,
            "format": img.format,
            "mode": img.mode,
            "size_bytes": p.stat().st_size,
        }
        exif = img.getexif()
        if exif:
            meta["exif"] = {k: str(v)[:100] for k, v in list(exif.items())[:20]}
        return meta


def _resize(p: Path, out: Path, w: int, h: int) -> None:
    from PIL import Image
    with Image.open(p) as img:
        img.resize((w, h), Image.LANCZOS).save(out)


def _crop(p: Path, out: Path, l: int, t: int, r: int, b: int) -> None:
    from PIL import Image
    with Image.open(p) as img:
        img.crop((l, t, r, b)).save(out)


def _convert(p: Path, out: Path, fmt: str) -> None:
    from PIL import Image
    with Image.open(p) as img:
        if fmt in ("jpg", "jpeg") and img.mode == "RGBA":
            img = img.convert("RGB")
        img.save(out, format=fmt.upper())


def _optimize(p: Path, out: Path, quality: int) -> None:
    from PIL import Image
    with Image.open(p) as img:
        fmt = img.format or "PNG"
        if fmt in ("JPEG", "JPG") and img.mode == "RGBA":
            img = img.convert("RGB")
        if fmt in ("JPEG", "JPG"):
            img.save(out, optimize=True, quality=quality)
        else:
            img.save(out, optimize=True)


def _favicon_set(p: Path, out_dir: Path) -> list[dict]:
    from PIL import Image
    sizes = [16, 32, 48, 64, 128, 256]
    results = []
    with Image.open(p) as img:
        for s in sizes:
            out = out_dir / f"favicon-{s}x{s}.png"
            img.resize((s, s), Image.LANCZOS).save(out)
            results.append({"size": s, "path": str(out)})
        # ICO
        try:
            ico_path = out_dir / "favicon.ico"
            img.save(
                ico_path,
                sizes=[(16, 16), (32, 32), (48, 48)],
            )
            results.append({"size": "ico", "path": str(ico_path)})
        except Exception:
            pass
    return results


def _visual_diff(a: Path, b: Path) -> dict:
    from PIL import Image
    import numpy as np

    with Image.open(a) as ia, Image.open(b) as ib:
        ia2 = ia.convert("RGB").resize((512, 512))
        ib2 = ib.convert("RGB").resize((512, 512))
        arr_a = np.asarray(ia2, dtype=float)
        arr_b = np.asarray(ib2, dtype=float)

    diff = np.abs(arr_a - arr_b)
    mean_diff = float(diff.mean())
    max_diff = float(diff.max())
    pct_diff = float((diff > 10).mean() * 100)

    result = {
        "mean_diff": round(mean_diff, 3),
        "max_diff": round(max_diff, 1),
        "percent_changed": round(pct_diff, 2),
        "identical": mean_diff < 1.0,
    }
    try:
        from skimage.metrics import structural_similarity
        ssim = float(structural_similarity(
            arr_a.astype(np.uint8),
            arr_b.astype(np.uint8),
            channel_axis=2,
        ))
        result["ssim"] = round(ssim, 4)
    except ImportError:
        pass
    return result


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
