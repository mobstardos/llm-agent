# -*- coding: utf-8 -*-
"""
Парсер HTTP (запрос/ответ) и самостоятельного JSON поверх TCP.

HTTP: накапливает заголовки + тело по Content-Length (или до конца
chunked-блока), извлекает метод/путь/код/заголовки, пытается разобрать
JSON-тело. JSON-режим: накопление до валидного JSON-объекта/массива.
"""

import json
import struct

from . import BaseFramer

NAME = "json_http"
LABEL = "HTTP / JSON"
PRIORITY = 70

HTTP_METHODS = (b"GET", b"POST", b"PUT", b"DELETE", b"PATCH", b"HEAD",
                b"OPTIONS", b"CONNECT", b"TRACE")
MAX_HEADER = 128 * 1024
MAX_BODY = 16 * 1024 * 1024
MAX_JSON = 8 * 1024 * 1024


def detect(chunk, ctx):
    if not chunk:
        return 0
    head = chunk[:256]
    for m in HTTP_METHODS:
        if head.startswith(m + b" "):
            return 92
    if head.startswith(b"HTTP/1.") and (b"\r\n\r\n" in chunk[:4096] or len(chunk) < 4096):
        return 92
    s = head.lstrip()
    if s[:1] in (b"{", b"["):
        return 65
    return 0


def create_framer(ctx):
    return HttpJsonFramer()


class HttpJsonFramer(BaseFramer):
    """Определяет режим по первым байтам: HTTP или JSON."""

    def __init__(self):
        BaseFramer.__init__(self)
        self.mode = None  # "http" | "json"

    def _guess_mode(self):
        if self.mode:
            return
        head = self.buf[:256]
        for m in HTTP_METHODS:
            if head.startswith(m + b" "):
                self.mode = "http"
                return
        if head.startswith(b"HTTP/1."):
            self.mode = "http"
            return
        if head.lstrip()[:1] in (b"{", b"["):
            self.mode = "json"
            return
        if len(self.buf) > 64:
            raise FrameError("не HTTP и не JSON")

    def try_extract(self):
        self._guess_mode()
        if self.mode == "http":
            return self._extract_http()
        return self._extract_json()

    # ---- HTTP ----

    def _extract_http(self):
        buf = self.buf
        sep = buf.find(b"\r\n\r\n")
        if sep == -1:
            if len(buf) > MAX_HEADER:
                raise FrameError("заголовки HTTP слишком большие")
            return None
        header_block = buf[:sep]
        body_start = sep + 4
        headers = {}
        try:
            lines = header_block.split(b"\r\n")
            first = lines[0]
            for ln in lines[1:]:
                if b":" in ln:
                    k, v = ln.split(b":", 1)
                    headers[k.strip().lower().decode("latin1")] = v.strip().decode("latin1")
        except Exception:
            pass
        cl = 0
        if "content-length" in headers:
            try:
                cl = int(headers["content-length"])
            except Exception:
                cl = 0
        if cl > MAX_BODY:
            raise FrameError("тело HTTP слишком большое: %d" % cl)
        if "transfer-encoding" in headers and "chunked" in headers["transfer-encoding"]:
            # ищем завершающий 0\r\n\r\n
            end = buf.find(b"0\r\n\r\n", body_start)
            if end == -1:
                if len(buf) > MAX_BODY:
                    raise FrameError("chunked-тело слишком большое")
                return None
            total = end + 5
        else:
            total = body_start + cl
        if len(buf) < total:
            return None
        frame = buf[:total]
        self.buf = buf[total:]
        self.last_http_meta = {"header_len": sep + 4, "first": first}
        return frame

    # ---- JSON (newline- или скобочно-сбалансированный) ----

    def _extract_json(self):
        buf = self.buf.lstrip()
        if not buf:
            self.buf = b""
            return None
        if buf[:1] not in (b"{", b"["):
            raise FrameError("не JSON")
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i, b in enumerate(buf):
            if esc:
                esc = False
                continue
            c = chr(b)
            if in_str:
                if c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            if i > MAX_JSON:
                raise FrameError("JSON слишком большой")
        if end == -1:
            if len(buf) > MAX_JSON:
                raise FrameError("JSON слишком большой (незакрытый)")
            return None
        frame = buf[:end + 1]
        rest = buf[end + 1:]
        # срезаем один разделитель (перевод строки)
        rest = rest[1:] if rest[:1] in (b"\n", b"\r") else rest
        self.buf = rest
        return frame


def parse(frame, ctx):
    framer_meta = getattr(ctx.get("_framer"), "last_http_meta", None)
    head = frame[:256]
    for m in HTTP_METHODS:
        if head.startswith(m + b" "):
            return _parse_http(frame)
    if head.startswith(b"HTTP/1."):
        return _parse_http(frame)
    return _parse_json(frame)


def _parse_http(frame):
    a = {"protocol": "HTTP", "valid": False}
    sep = frame.find(b"\r\n\r\n")
    if sep == -1:
        a["notes"] = "нет завершения заголовков"
        a["summary"] = "HTTP (незавершённый)"
        return a
    header_block = frame[:sep].decode("latin1", errors="replace")
    body = frame[sep + 4:]
    lines = header_block.split("\r\n")
    request_line = lines[0]
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    a["valid"] = True
    if request_line.upper().startswith("HTTP/"):
        parts = request_line.split(" ", 2)
        a["http_code"] = parts[1] if len(parts) > 1 else ""
        a["http_method"] = "RESPONSE"
        a["http_path"] = parts[2] if len(parts) > 2 else ""
        a["direction_label"] = "ответ"
    else:
        parts = request_line.split(" ")
        a["http_method"] = parts[0]
        a["http_path"] = parts[1] if len(parts) > 1 else ""
        a["http_version"] = parts[2] if len(parts) > 2 else ""
        a["direction_label"] = "запрос"
        if "?" in a["http_path"]:
            a["query"] = a["http_path"].split("?", 1)[1]
    a["headers"] = {k: v for k, v in list(headers.items())[:20]}
    if body:
        a["body_size"] = len(body)
        try:
            j = json.loads(body.decode("utf-8", errors="strict"))
            a["json"] = True
            if isinstance(j, dict):
                a["json_keys"] = list(j.keys())[:20]
                a["json_sample"] = str(j)[:300]
        except Exception:
            printable = body[:200].decode("utf-8", errors="replace").strip()
            if printable:
                a["body_preview"] = printable
    parts = []
    parts.append(str(a.get("http_method", "")))
    if a.get("http_path"):
        parts.append(a["http_path"].split("?")[0])
    if a.get("http_code"):
        parts.append("-> %s" % a["http_code"])
    if a.get("json"):
        parts.append("(json %sБ)" % len(body))
    a["summary"] = " ".join(p for p in parts if p)
    return a


def _parse_json(frame):
    a = {"protocol": "JSON", "valid": False}
    try:
        j = json.loads(frame.decode("utf-8", errors="strict"))
    except Exception as e:
        a["notes"] = "JSON не разобран: %s" % e
        a["summary"] = "JSON (невалидный)"
        return a
    a["valid"] = True
    a["json"] = True
    if isinstance(j, dict):
        a["json_keys"] = list(j.keys())[:20]
        for key in ("method", "action", "command", "cmd", "type", "event"):
            if key in j and isinstance(j[key], (str, int)):
                a["json_method"] = str(j[key])
                break
        if "id" in j:
            a["json_id"] = j["id"]
    elif isinstance(j, list):
        a["json_items"] = len(j)
    a["json_sample"] = json.dumps(j, ensure_ascii=False)[:300]
    parts = []
    if a.get("json_method"):
        parts.append(str(a["json_method"]))
    if a.get("json_id") is not None:
        parts.append("id=%s" % a["json_id"])
    if a.get("json_items") is not None:
        parts.append("массив[%s]" % a["json_items"])
    if not parts:
        parts.append("ключи: %s" % ",".join(a.get("json_keys", [])[:6]))
    a["summary"] = " ".join(str(p) for p in parts)
    return a
