#!/usr/bin/env python3
"""Генерирует PNG-иконки расширения (16/32/48/128) без PIL.

Рисунок: тёмно-синий скруглённый квадрат + светлая «мост-дуга» и
точка (стилизованный мост). Чистый zlib/struct — PNG собирается
вручную (RGBA, без сжатия фильтров кроме filter=0 на строку).
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png(width: int) -> bytes:
    px = bytearray()
    r2 = (width / 2) ** 2
    for y in range(width):
        px.append(0)  # filter: None
        for x in range(width):
            # скруглённый квадрат
            dx, dy = x + 0.5 - width / 2, y + 0.5 - width / 2
            inside = dx * dx + dy * dy <= r2 * 0.92
            if not inside:
                px += bytes((0, 0, 0, 0))
                continue
            # фон: вертикальный градиент #2c4a7c → #1a2f52
            k = y / max(1, width - 1)
            r, g, b = int(44 + (26 - 44) * k), int(74 + (47 - 74) * k), \
                int(124 + (82 - 124) * k)
            # «мост»: дуга y ~ sin(x)
            t = (x - width / 2) / (width / 2)          # -1..1
            arc_y = width * 0.68 - abs(t) * width * 0.30
            pil_y = width * 0.62
            on_arc = abs(y - arc_y) < max(1.6, width * 0.055)
            on_pil = abs(y - pil_y) < max(1.2, width * 0.045) \
                and abs(t) < 0.62
            # столб: x центр
            on_post = abs(x - width / 2) < max(1.2, width * 0.045) \
                and arc_y < y < pil_y
            if on_arc or on_pil or on_post:
                r, g, b, a = 120, 190, 250, 255
            else:
                a = 255
            px += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", width, width, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(px), 9))
            + _chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        (OUT / f"icon{size}.png").write_bytes(png(size))
        print(f"icon{size}.png OK")


if __name__ == "__main__":
    main()
