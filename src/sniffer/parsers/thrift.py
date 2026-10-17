# -*- coding: utf-8 -*-
"""
Парсер Thrift RPC (TFramedTransport + TBinaryProtocol, strict и non-strict).

Помимо имени метода/типа сообщения — луч-effort разбор полей структуры
(TBinaryProtocol: field-type:1, field-id:2 BE): строки, i32/i64, double,
bool, вложенность первого уровня. Позволяет видеть аргументы вызова.
"""

import struct

from . import BaseFramer

NAME = "thrift"
LABEL = "Thrift RPC"
PRIORITY = 85

FRAME_HEADER = 4

THRIFT_METHODS = {
    b"onConnect": "ON_CONNECT",
    b"sendActivity": "SEND_ACTIVITY",
    b"sendTanksState": "SEND_TANKS_STATE",
    b"sendCalibrationSamples": "SEND_CALIBRATION",
    b"cashless_ActionStart": "CASHLESS_START",
    b"cashless_ActionRollback": "CASHLESS_ROLLBACK",
    b"cashless_ActionCommit": "CASHLESS_COMMIT",
    b"discount_ActionStartWInf": "DISCOUNT_START",
    b"discount_ActionCommit": "DISCOUNT_COMMIT",
    b"syncData": "SYNC_DATA",
    b"sendCounterState": "SEND_COUNTERS",
    b"sendPrices": "SEND_PRICES",
    b"sendShiftState": "SEND_SHIFT",
}

MSG_TYPES = {1: "CALL", 2: "REPLY", 3: "EXCEPTION", 4: "ONEWAY"}

FIELD_TYPES = {0: "STOP", 2: "BOOL", 3: "BYTE", 4: "DOUBLE", 6: "I16",
               8: "I32", 10: "I64", 11: "STRING", 12: "STRUCT",
               13: "MAP", 14: "SET", 15: "LIST"}

SIMPLE_SIZES = {2: 1, 3: 1, 4: 8, 6: 2, 8: 4, 10: 8}


def detect(chunk, ctx):
    if len(chunk) < 6:
        return 0  # мало данных — решение отложим
    n = struct.unpack(">I", chunk[:4])[0]
    if n == 0 or n > 32 * 1024 * 1024:
        return 0
    payload = chunk[4:4 + min(n, 64)]
    if not payload:
        return 10
    # strict binary: 0x80 0x01, затем тип сообщения
    if payload[0] == 0x80 and len(payload) > 3 and payload[1] == 0x01 and payload[3] in (1, 2, 3, 4):
        return 95
    # non-strict: первый байт — тип сообщения
    if payload[0] in (1, 2, 3, 4) and 5 + 4 <= len(payload):
        name_len = struct.unpack(">I", payload[1:5])[0]
        if 0 < name_len < 256 and 5 + name_len <= len(payload):
            try:
                payload[5:5 + name_len].decode("ascii")
                return 95
            except Exception:
                return 40
    return 5


def create_framer(ctx):
    return ThriftFramer()


class ThriftFramer(BaseFramer):
    def try_extract(self):
        buf = self.buf
        if len(buf) < FRAME_HEADER:
            return None
        n = struct.unpack(">I", buf[:4])[0]
        if n == 0 or n > self.max_frame:
            raise FrameError("неверная длина Thrift-фрейма: %d" % n)
        if len(buf) < FRAME_HEADER + n:
            return None
        frame = buf[:FRAME_HEADER + n]
        self.buf = buf[FRAME_HEADER + n:]
        return frame


def _read_struct_fields(payload, max_fields=24):
    """Разбирает поля TBinaryProtocol-структуры (плоско + один уровень вложенности)."""
    fields = []
    i = 0
    n = len(payload)
    while i < n and len(fields) < max_fields:
        ftype = payload[i]
        if ftype == 0:  # STOP
            i += 1
            break
        if i + 3 > n or ftype not in FIELD_TYPES or ftype == 0:
            break
        fid = struct.unpack(">h", payload[i + 1:i + 3])[0]
        i += 3
        entry = {"id": fid, "type": FIELD_TYPES[ftype]}
        if ftype in SIMPLE_SIZES:
            raw = payload[i:i + SIMPLE_SIZES[ftype]]
            if len(raw) < SIMPLE_SIZES[ftype]:
                break
            i += SIMPLE_SIZES[ftype]
            if ftype == 2:
                entry["value"] = bool(raw[0])
            elif ftype == 3:
                entry["value"] = struct.unpack(">b", raw)[0]
            elif ftype == 4:
                entry["value"] = struct.unpack(">d", raw)[0]
            elif ftype == 6:
                entry["value"] = struct.unpack(">h", raw)[0]
            elif ftype == 8:
                entry["value"] = struct.unpack(">i", raw)[0]
            elif ftype == 10:
                entry["value"] = struct.unpack(">q", raw)[0]
        elif ftype == 11:
            if i + 4 > n:
                break
            slen = struct.unpack(">I", payload[i:i + 4])[0]
            i += 4
            if slen > n - i or slen > 4096:
                entry["value"] = "<строка %d байт>" % slen if slen <= 16 * 1024 * 1024 else "<мусор>"
                break
            try:
                entry["value"] = payload[i:i + slen].decode("utf-8", errors="replace")
            except Exception:
                entry["value"] = repr(payload[i:i + slen][:40])
            i += slen
        elif ftype == 12:  # STRUCT — один уровень вложенности, читаем до STOP
            sub = []
            j = i
            while j < n and len(sub) < 8:
                st = payload[j]
                if st == 0:
                    j += 1
                    break
                if j + 3 > n:
                    break
                sid = struct.unpack(">h", payload[j + 1:j + 3])[0]
                j += 3
                if st in SIMPLE_SIZES:
                    raw = payload[j:j + SIMPLE_SIZES[st]]
                    if len(raw) < SIMPLE_SIZES[st]:
                        j = n
                        break
                    j += SIMPLE_SIZES[st]
                    try:
                        sub.append({"id": sid, "type": FIELD_TYPES.get(st, st),
                                    "value": struct.unpack({3: ">b", 6: ">h", 8: ">i",
                                                            10: ">q"}.get(st), raw)[0]})
                    except Exception:
                        sub.append({"id": sid, "type": FIELD_TYPES.get(st, st)})
                elif st == 11:
                    if j + 4 > n:
                        break
                    slen = struct.unpack(">I", payload[j:j + 4])[0]
                    j += 4
                    if slen > n - j:
                        break
                    sub.append({"id": sid, "type": "STRING",
                                "value": payload[j:j + slen].decode("utf-8", errors="replace")[:120]})
                    j += slen
                else:
                    break
            entry["fields"] = sub
            i = j
        else:
            # LIST/SET/MAP — глубже не идём, отмечаем наличие
            entry["value"] = "<коллекция>"
            break
        fields.append(entry)
        if i <= 0:
            break
    return fields


def parse(frame, ctx):
    a = {"protocol": "THRIFT", "valid": False}
    if len(frame) < FRAME_HEADER + 1:
        a["notes"] = "короткий кадр"
        return a
    n = struct.unpack(">I", frame[:4])[0]
    a["frame_len"] = n
    payload = frame[FRAME_HEADER:FRAME_HEADER + n]
    if len(payload) < 1:
        return a

    off = 0
    strict = False
    if payload[0] == 0x80 and len(payload) >= 6 and payload[1] == 0x01:
        strict = True
        msg_type = payload[3]
        off = 4
    else:
        msg_type = payload[0]
        off = 1
    a["msg_type"] = MSG_TYPES.get(msg_type, "UNKNOWN_%s" % msg_type)

    if len(payload) < off + 4:
        a["notes"] = "нет заголовка метода"
        return a
    name_len = struct.unpack(">I", payload[off:off + 4])[0]
    off += 4
    if name_len == 0 or name_len > 4096 or off + name_len > len(payload):
        a["notes"] = "некорректная длина имени метода (%d)" % name_len
        return a
    try:
        method = payload[off:off + name_len].decode("utf-8", errors="replace")
    except Exception:
        method = repr(payload[off:off + name_len][:40])
    a["method"] = method
    a["valid"] = True
    off += name_len
    if off + 4 <= len(payload):
        a["seq_id"] = struct.unpack(">I", payload[off:off + 4])[0]
        off += 4

    for pattern, label in THRIFT_METHODS.items():
        if pattern in method.encode("utf-8", errors="ignore") or pattern in method.encode("latin1", errors="ignore"):
            a["method_type"] = label
            break

    # Аргументы: после заголовка сообщения идёт структура аргументов
    rest = payload[off:]
    if rest:
        try:
            fields = _read_struct_fields(rest)
            if fields:
                a["fields"] = fields
        except Exception:
            pass

    strings = _extract_strings(payload)
    if strings:
        a["strings"] = strings

    parts = [a["msg_type"], method]
    if a.get("seq_id") is not None:
        parts.append("seq=%s" % a["seq_id"])
    if a.get("method_type"):
        parts.append("[%s]" % a["method_type"])
    a["summary"] = " ".join(parts)
    return a


def _extract_strings(payload, limit=8):
    out = []
    cur = b""
    for b in payload:
        if 32 <= b <= 126:
            cur += bytes([b])
        else:
            if len(cur) >= 4:
                try:
                    s = cur.decode("ascii")
                    if s not in out and not s.startswith("0."):
                        out.append(s)
                except Exception:
                    pass
            cur = b""
            if len(out) >= limit:
                break
    if len(cur) >= 4 and len(out) < limit:
        try:
            out.append(cur.decode("ascii"))
        except Exception:
            pass
    return out[:limit]
