# -*- coding: utf-8 -*-
"""
Парсер Modbus TCP: MBAP-заголовок [txid:2][proto:2=0][len:2][unit:1] + PDU.
Извлекает функцию, адрес регистра, количество и значения; распознаёт
исключения Modbus.
"""

import struct

from . import BaseFramer

NAME = "modbus"
LABEL = "Modbus TCP"
PRIORITY = 80

MBAP = 7

FUNCTIONS = {
    1: "Read Coils", 2: "Read Discrete Inputs", 3: "Read Holding Registers",
    4: "Read Input Registers", 5: "Write Single Coil", 6: "Write Single Register",
    7: "Read Exception Status", 8: "Diagnostics", 11: "Get Comm Event Counter",
    12: "Get Comm Event Log", 15: "Write Multiple Coils",
    16: "Write Multiple Registers", 17: "Report Slave ID",
    22: "Mask Write Register", 23: "Read/Write Multiple Registers",
    43: "Read Device Identification",
}
EXCEPTIONS = {
    1: "Illegal Function", 2: "Illegal Data Address", 3: "Illegal Data Value",
    4: "Slave Device Failure", 5: "Acknowledge", 6: "Slave Device Busy",
    8: "Memory Parity Error", 10: "Gateway Path Unavailable",
    11: "Gateway Target Failed",
}


def detect(chunk, ctx):
    if len(chunk) < 8:
        return 0
    proto = struct.unpack(">H", chunk[2:4])[0]
    ln = struct.unpack(">H", chunk[4:6])[0]
    if proto != 0:
        return 0
    if ln < 2 or ln > 260:
        return 0
    if ln + 6 > len(chunk) + 4:   # len = unit + PDU; кадр = 6 + ln
        return 0
    fc = chunk[7] if len(chunk) > 7 else 0
    if fc <= 0x7F or fc >= 0x81:
        return 88
    return 0


def create_framer(ctx):
    return ModbusFramer()


class ModbusFramer(BaseFramer):
    def try_extract(self):
        buf = self.buf
        if len(buf) < MBAP:
            return None
        proto = struct.unpack(">H", buf[2:4])[0]
        if proto != 0:
            raise FrameError("protocol id != 0")
        ln = struct.unpack(">H", buf[4:6])[0]
        if ln < 2 or ln > 260:
            raise FrameError("неверная длина PDU: %d" % ln)
        total = 6 + ln
        if len(buf) < total:
            return None
        frame = buf[:total]
        self.buf = buf[total:]
        return frame


def parse(frame, ctx):
    a = {"protocol": "MODBUS", "valid": False}
    if len(frame) < MBAP:
        a["notes"] = "короткий кадр"
        return a
    txid = struct.unpack(">H", frame[0:2])[0]
    unit = frame[6]
    a["txid"] = txid
    a["unit_id"] = unit
    if len(frame) < 8:
        a["notes"] = "нет PDU"
        return a
    fc = frame[7]
    pdu = frame[8:]
    a["valid"] = True

    if fc >= 0x80:
        a["function"] = "EXCEPTION (fc=%d)" % (fc - 0x80)
        if pdu:
            a["exception"] = EXCEPTIONS.get(pdu[0], "code %d" % pdu[0])
        a["summary"] = "unit=%d %s %s" % (unit, a["function"], a.get("exception", ""))
        return a

    a["function"] = FUNCTIONS.get(fc, "fc=%d" % fc)
    # запросы чтения: адрес(2) + количество(2)
    if fc in (1, 2, 3, 4, 5, 6) and len(pdu) >= 4:
        addr, qty = struct.unpack(">HH", pdu[:4])
        a["address"] = addr
        a["quantity"] = qty
    elif fc in (15, 16) and len(pdu) >= 5:
        addr, qty = struct.unpack(">HH", pdu[:4])
        a["address"] = addr
        a["quantity"] = qty
        a["byte_count"] = pdu[4]
    elif fc == 23 and len(pdu) >= 9:
        ra, rq, wa, wq = struct.unpack(">HHHH", pdu[:8])
        a["address"] = ra
        a["quantity"] = rq
        a["write_address"] = wa
        a["write_quantity"] = wq
    # ответы чтения регистров: byte_count + данные
    elif fc in (3, 4) and pdu and not a.get("quantity"):
        bc = pdu[0]
        vals = [struct.unpack(">H", pdu[1 + i * 2:3 + i * 2])[0]
                for i in range(min(bc // 2, 32))] if len(pdu) >= 1 + bc else []
        if vals:
            a["values"] = vals
    # ответы чтения битовых объектов
    elif fc in (1, 2) and pdu:
        bc = pdu[0]
        if bc and len(pdu) >= 1 + bc:
            bits = []
            for i in range(min(bc * 8, 32)):
                bits.append((pdu[1 + i // 8] >> (i % 8)) & 1)
            a["values"] = bits

    parts = ["unit=%d" % unit, a["function"]]
    if a.get("address") is not None:
        parts.append("addr=%s" % a["address"])
    if a.get("quantity") is not None:
        parts.append("qty=%s" % a["quantity"])
    if a.get("values"):
        parts.append("vals=%s" % a["values"][:8])
    a["summary"] = " ".join(parts)
    return a
