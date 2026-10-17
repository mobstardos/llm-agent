# -*- coding: utf-8 -*-
"""
Резервный парсер: hex-дамп + ASCII-вид + извлечение печатных строк.
Работает в режиме "кусками" (create_framer отсутствует).
"""

NAME = "hexdump"
LABEL = "Hex / RAW"
PRIORITY = 1


def detect(chunk, ctx):
    return 10  # всегда чуть-чуть подходит — последний в приоритете


def parse(chunk, ctx):
    a = {"protocol": "RAW", "valid": True}
    a["hexdump"] = hexdump(chunk[:2048])
    strings = printable_strings(chunk)
    if strings:
        a["strings"] = strings
    binary_ratio = _binary_ratio(chunk)
    a["binary_ratio"] = round(binary_ratio, 2)
    parts = []
    if strings:
        parts.append("строки: %s" % " | ".join(strings[:3]))
    kind = "текст" if binary_ratio < 0.15 else ("бинарные данные" if binary_ratio > 0.6 else "смешанные")
    parts.append(kind)
    a["summary"] = " ".join(parts)
    return a


def hexdump(data, width=16):
    """Классический hex-дамп: смещение, hex, ascii."""
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexpart = " ".join("%02X" % b for b in chunk)
        asciipart = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append("%04X  %-47s  |%s|" % (off, hexpart, asciipart))
    suffix = ""
    if len(data) > 2048:
        suffix = "\n... (%d байт всего, показано 2048)" % len(data)
    return "\n".join(lines) + suffix


def printable_strings(data, min_len=4, limit=10):
    out = []
    cur = b""
    for b in data:
        if 32 <= b <= 126 or b in (9, 10, 13):
            cur += bytes([b])
        else:
            if len(cur) >= min_len:
                out.append(cur.decode("ascii", errors="replace").strip())
                if len(out) >= limit:
                    return out
            cur = b""
    if len(cur) >= min_len and len(out) < limit:
        out.append(cur.decode("ascii", errors="replace").strip())
    return [s for s in out if s]


def _binary_ratio(data):
    if not data:
        return 0.0
    sample = data[:4096]
    printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
    return 1.0 - printable / len(sample)
