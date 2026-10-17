# -*- coding: utf-8 -*-
"""
Парсер TCP-протокола RemoteServer (АЗС): заголовок 9 байт
[version:1][payload_len:4 LE][magic:4 = AA AA 00 00], payload часто zlib.
Извлекает RequestCmdClass/RequestCmd/SessionNum/TransactionID, таблицы БД.
"""

import struct
import zlib

from . import BaseFramer

NAME = "remote_server"
LABEL = "RemoteServer TCP"
PRIORITY = 90

HEADER = 9
MAGIC = b"\xAA\xAA\x00\x00"

CMD_TYPES = {
    (0, 1): "HANDSHAKE",
    (0, 2): "SERVICE",
    (50, 5): "REPL_UP_1",
    (50, 6): "REPL_UP_2",
    (200, 1): "SESSION_VAR",
    (200, 2): "EXTRA_REQ",
    (200, 3): "QUICK_NO_DB",
    (200, 4): "EXTRA_REQ_2",
    (200, 5): "EXTRA_REQ_3",
    (200, 100): "DB_REQUEST",
}

KNOWN_TABLES = [
    "sysUsers", "sysSessions", "sysTokens", "sysExchangeLog",
    "dcDiscountCard", "dcCards", "dcPartners", "dcAmounts",
    "rgAmountRests", "rgDiscount", "dcAzs", "dcSessions",
    "sysComponentVersions", "sysDeletedObjects", "dcTanks", "dcNozzles",
    "azsShifts", "opOperations", "dcPrice", "rgPrices",
]


def detect(chunk, ctx):
    if len(chunk) < HEADER:
        return 0  # мало данных — решение отложим до накопления
    if chunk[5:9] == MAGIC:
        plen = struct.unpack("<I", chunk[1:5])[0]
        if plen < 32 * 1024 * 1024:
            return 98
        return 70
    return 0


def create_framer(ctx):
    return RemoteServerFramer()


class RemoteServerFramer(BaseFramer):
    def try_extract(self):
        buf = self.buf
        if len(buf) < HEADER:
            return None
        if buf[5:9] != MAGIC:
            raise FrameError("магия RemoteServer не найдена")
        plen = struct.unpack("<I", buf[1:5])[0]
        if plen > self.max_frame:
            raise FrameError("payload_len слишком велик: %d" % plen)
        total = HEADER + plen
        if len(buf) < total:
            return None
        frame = buf[:total]
        self.buf = buf[total:]
        return frame


def parse(frame, ctx):
    a = {"protocol": "REMOTE_SERVER", "valid": False}
    if len(frame) < HEADER:
        a["notes"] = "короткий кадр"
        return a
    a["version"] = frame[0]
    plen = struct.unpack("<I", frame[1:5])[0]
    a["payload_len"] = plen
    payload = frame[HEADER:HEADER + plen]
    a["compressed"] = False

    data = None
    if payload[:2] == b"\x78\xDA" or payload[:1] in (b"\x78", b"\x58"):
        a["compressed"] = True
        try:
            data = zlib.decompress(payload)
        except Exception:
            try:
                data = zlib.decompressobj().decompress(payload)
            except Exception:
                data = None
    if data is None:
        data = payload
        a["compressed"] = False

    a["valid"] = True
    if data:
        a["decompressed_size"] = len(data)

    # Извлечение полей по текстовым меткам (формат RemoteServer).
    # Важно: "RequestCmd" входит в "RequestCmdClass" — ищем вхождение,
    # которое НЕ является началом более длинной метки.
    def _find_label(hay, label, longer):
        start = 0
        while True:
            idx = hay.find(label, start)
            if idx == -1:
                return -1
            # пропускаем вхождение, только если здесь начинается ДРУГАЯ,
            # более длинная метка (RequestCmd внутри RequestCmdClass)
            if longer == label or not longer or hay[idx:idx + len(longer)] != longer:
                return idx
            start = idx + 1

    for label, longer, name, fmt, width in (
        (b"RequestCmdClass", None, "cmd_class", ">H", 2),
        (b"RequestCmd", b"RequestCmdClass", "cmd", ">H", 2),
        (b"SessionNum", None, "session", ">I", 4),
        (b"TransactionID", None, "txid", ">I", 4),
    ):
        idx = _find_label(data, label, longer)
        if idx != -1:
            start = idx + len(label)
            chunk = data[start:start + width]
            if len(chunk) == width:
                try:
                    a[name] = struct.unpack(fmt, chunk)[0]
                except Exception:
                    pass

    if a.get("cmd_class") is not None and a.get("cmd") is not None:
        key = (a["cmd_class"], a["cmd"])
        a["cmd_type"] = CMD_TYPES.get(key, "UNKNOWN_%s_%s" % key)

    tables = [t for t in KNOWN_TABLES if t.encode() in data]
    if tables:
        a["tables"] = tables

    # Короткое человекочитаемое описание
    parts = []
    if a.get("cmd_type"):
        parts.append(a["cmd_type"])
        parts.append("(C=%s,D=%s)" % (a.get("cmd_class"), a.get("cmd")))
    elif a.get("cmd_class") is not None:
        parts.append("C=%s" % a["cmd_class"])
    if a.get("session") is not None:
        parts.append("S=%s" % a["session"])
    if a.get("txid") is not None:
        parts.append("TX=%s" % a["txid"])
    if a.get("compressed"):
        parts.append("zlib")
    a["summary"] = " ".join(parts) if parts else "RemoteServer кадр"
    return a
