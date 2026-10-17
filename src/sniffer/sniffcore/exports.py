# -*- coding: utf-8 -*-
"""
Экспорт перехваченного трафика:
  * JSONL      — построчный JSON с ротацией по размеру
  * SQLite     — база packets/sessions (запись фоновым потоком пачками)
  * PCAP       — дамп, открывающийся в Wireshark (синтетические IP/TCP-заголовки)
  * CSV        — отчёты по сессиям и частотности команд
  * TXT        — человекочитаемые логи по клиентам (опционально)

Все приёмники независимы: ошибка одного не влияет на перехват и другие.
"""

import csv
import json
import os
import queue
import socket
import sqlite3
import struct
import threading
import time
from datetime import datetime


def _ts_name(prefix, ext):
    return "%s_%s.%s" % (prefix, datetime.now().strftime("%Y%m%d_%H%M%S"), ext)


def _rotate(folder, prefix, ext, keep):
    """Удаляет самые старые ротированные файлы, оставляя keep последних."""
    try:
        files = sorted(
            (f for f in os.listdir(folder)
             if f.startswith(prefix + "_") and f.endswith("." + ext)),
            key=lambda f: os.path.getmtime(os.path.join(folder, f)))
        for f in files[:-keep] if keep > 0 else files:
            os.remove(os.path.join(folder, f))
    except Exception:
        pass


class JsonlWriter:
    def __init__(self, path, rotation_mb=50, keep=5, on_error=None):
        self.path = path
        self.rotation = int(rotation_mb) * 1024 * 1024
        self.keep = int(keep)
        self.on_error = on_error
        self.bytes_written = 0
        self.count = 0
        self._fh = None
        self._lock = threading.Lock()
        self._ensure_dir()
        self._open()

    def _ensure_dir(self):
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)

    def _open(self):
        self._fh = open(self.path, "a", encoding="utf-8")
        self.bytes_written = os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def _rotate(self):
        try:
            if self._fh:
                self._fh.close()
            newname = _ts_name("traffic", "jsonl")
            folder = os.path.dirname(os.path.abspath(self.path))
            os.replace(self.path, os.path.join(folder, newname))
            _rotate(folder, "traffic", "jsonl", self.keep)
        except Exception as e:
            if self.on_error:
                self.on_error("JSONL rotate: %s" % e)
        self._open()

    def write(self, analysis):
        entry = {
            "timestamp": analysis.get("timestamp"),
            "protocol": analysis.get("protocol"),
            "direction": analysis.get("direction"),
            "client": analysis.get("client"),
            "client_port": analysis.get("client_port"),
            "port": analysis.get("listen_port"),
            "port_name": analysis.get("port_name"),
            "size": analysis.get("size"),
            "valid": analysis.get("valid"),
        }
        for k in ("cmd_type", "cmd_class", "cmd", "session", "txid", "tables",
                  "decompressed_size", "msg_type", "method", "method_type", "seq_id",
                  "frame_len", "strings", "fields", "http_method", "http_path",
                  "http_code", "unit_id", "function", "address", "quantity", "values",
                  "byte_count", "write_address", "write_quantity", "exception",
                  "json_keys", "json_method", "json_id", "json_items", "body_size",
                  "summary", "notes", "parse_error"):
            if analysis.get(k) is not None:
                entry[k] = analysis[k]
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
                self.bytes_written += len(line) + 1
                self.count += 1
                if self.rotation > 0 and self.bytes_written >= self.rotation:
                    self._rotate()
            except Exception as e:
                if self.on_error:
                    self.on_error("JSONL write: %s" % e)

    def close(self):
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
                    self._fh = None
            except Exception:
                pass


# ---------------------------------------------------------------- SQLite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, ts_iso TEXT, conn_id INTEGER,
    port_name TEXT, listen_port INTEGER,
    client TEXT, client_port INTEGER, direction TEXT,
    protocol TEXT, valid INTEGER, size INTEGER,
    type TEXT, summary TEXT, data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_packets_ts ON packets(ts);
CREATE INDEX IF NOT EXISTS idx_packets_proto ON packets(protocol);
CREATE INDEX IF NOT EXISTS idx_packets_type ON packets(type);
CREATE TABLE IF NOT EXISTS sessions (
    conn_id INTEGER PRIMARY KEY,
    client TEXT, client_port INTEGER,
    port_name TEXT, listen_port INTEGER,
    target TEXT, started TEXT, ended TEXT,
    packets_tx INTEGER, packets_rx INTEGER,
    bytes_tx INTEGER, bytes_rx INTEGER,
    state TEXT, close_reason TEXT
);
"""


class SqliteWriter:
    """Фоновый писатель: пачками, чтобы не тормозить перехват."""

    def __init__(self, path, flush_interval=1.0, retention_rows=500000, on_error=None):
        self.path = path
        self.flush_interval = flush_interval
        self.retention = int(retention_rows)
        self.on_error = on_error
        self.q = queue.Queue(maxsize=20000)
        self.enabled = True
        self.count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sqlite-writer")
        try:
            d = os.path.dirname(os.path.abspath(path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.executescript(_SCHEMA)
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            self.conn.commit()
            self._thread.start()
        except Exception as e:
            self.enabled = False
            if self.on_error:
                self.on_error("SQLite отключён: %s" % e)

    def write_packet(self, analysis, pid):
        if not self.enabled:
            return
        ptype = analysis.get("cmd_type") or analysis.get("method_type")
        row = (pid, analysis.get("ts"), analysis.get("timestamp"), analysis.get("conn_id"),
               analysis.get("port_name"), analysis.get("listen_port"),
               analysis.get("client"), analysis.get("client_port"), analysis.get("direction"),
               analysis.get("protocol"), 1 if analysis.get("valid") else 0, analysis.get("size"),
               ptype, analysis.get("summary", ""),
               json.dumps({k: analysis[k] for k in analysis
                           if k in ("tables", "strings", "fields", "http_method", "http_path",
                                    "http_code", "unit_id", "function", "address", "quantity",
                                    "values", "frame_len", "decompressed_size", "cmd_class",
                                    "cmd", "session", "txid", "method", "msg_type", "seq_id")
                           and analysis[k] is not None}, ensure_ascii=False, default=str))
        try:
            self.q.put_nowait(("packet", row))
        except queue.Full:
            pass

    def write_session(self, s):
        if not self.enabled:
            return
        try:
            self.q.put_nowait(("session", (
                s.get("conn_id"), s.get("client"), s.get("client_port"),
                s.get("port_name"), s.get("listen_port"),
                "%s:%s" % (s.get("target_host"), s.get("target_port")),
                s.get("started_iso"), s.get("end_iso"),
                s.get("packets_tx", 0), s.get("packets_rx", 0),
                s.get("bytes_tx", 0), s.get("bytes_rx", 0),
                s.get("state", "closed"), s.get("close_reason", ""))))
        except queue.Full:
            pass

    def _loop(self):
        batch_p, batch_s = [], []
        next_flush = time.time() + self.flush_interval
        while not self._stop.is_set() or not self.q.empty():
            try:
                kind, row = self.q.get(timeout=0.2)
                if kind == "packet":
                    batch_p.append(row)
                else:
                    batch_s.append(row)
            except queue.Empty:
                pass
            except Exception:
                pass
            if time.time() >= next_flush and (batch_p or batch_s):
                try:
                    if batch_p:
                        self.conn.executemany(
                            "INSERT INTO packets (id, ts, ts_iso, conn_id, port_name, listen_port,"
                            " client, client_port, direction, protocol, valid, size, type,"
                            " summary, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch_p)
                        self.count += len(batch_p)
                        batch_p = []
                    if batch_s:
                        self.conn.executemany(
                            "INSERT OR REPLACE INTO sessions (conn_id, client, client_port,"
                            " port_name, listen_port, target, started, ended, packets_tx,"
                            " packets_rx, bytes_tx, bytes_rx, state, close_reason)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch_s)
                        batch_s = []
                    self.conn.commit()
                    if self.retention > 0 and self.count > self.retention:
                        cut = self.count - self.retention
                        self.conn.execute("DELETE FROM packets WHERE id <="
                                          " (SELECT MIN(id) FROM (SELECT id FROM packets"
                                          " ORDER BY id LIMIT 1 OFFSET ?))", (cut - 1,))
                        self.conn.commit()
                        self.count = self.retention
                except Exception as e:
                    if self.on_error:
                        self.on_error("SQLite write: %s" % e)
                next_flush = time.time() + self.flush_interval

    def close(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            if self.enabled:
                self.conn.commit()
                self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- PCAP

def _ip2b(ip):
    try:
        return socket.inet_aton(ip)
    except Exception:
        return socket.inet_aton("127.0.0.1")


def _checksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) + data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


class PcapWriter:
    """Пишет классический pcap (LINKTYPE_RAW) с синтетическими IP/TCP-заголовками,
    чтобы Wireshark корректно разбирал поток как TCP."""

    def __init__(self, path, rotation_mb=100, keep=5, on_error=None):
        self.path = path
        self.rotation = int(rotation_mb) * 1024 * 1024
        self.keep = int(keep)
        self.on_error = on_error
        self.enabled = True
        self.count = 0
        self.bytes_written = 0
        self._ip_id = 1
        self._seq = {}
        self._lock = threading.Lock()
        self._open()

    def _open(self):
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            self._fh = open(self.path, "wb")
            self._fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 101))
            self.bytes_written = 24
        except Exception as e:
            self.enabled = False
            if self.on_error:
                self.on_error("PCAP отключён: %s" % e)

    def _tcp_ip(self, payload, src_ip, src_port, dst_ip, dst_port, direction, conn_id):
        seq_key = (conn_id, direction)
        seq = self._seq.get(seq_key, 1000)
        self._seq[seq_key] = seq + max(len(payload), 1)
        # TCP: заголовок без контрольной суммы, затем досчёт по псевдозаголовку
        tcp = struct.pack(">HHIIBBHHH", src_port, dst_port, seq, seq + 1, 0x50, 0x18, 65535, 0, 0)
        pseudo = _ip2b(src_ip) + _ip2b(dst_ip) + struct.pack(">BBH", 0, 6, len(tcp) + len(payload))
        csum = _checksum(pseudo + tcp + payload)
        tcp = struct.pack(">HHIIBBHHH", src_port, dst_port, seq, seq + 1, 0x50, 0x18, 65535, csum, 0)
        # IP
        total_len = 20 + len(tcp) + len(payload)
        self._ip_id = (self._ip_id + 1) & 0xFFFF
        ip_hdr = struct.pack(">BBHHHBBH", 0x45, 0, total_len, self._ip_id, 0x4000, 64, 6, 0) + \
            _ip2b(src_ip) + _ip2b(dst_ip)
        ip_csum = _checksum(ip_hdr)
        ip_hdr = ip_hdr[:10] + struct.pack(">H", ip_csum) + ip_hdr[12:]
        return ip_hdr + tcp + payload

    def write(self, analysis, raw):
        if not self.enabled or raw is None:
            return
        try:
            ts = analysis.get("ts") or time.time()
            src_ip = analysis.get("client") or "0.0.0.0"
            dst_ip = analysis.get("target_host") or "127.0.0.1"
            if analysis.get("direction") == "TX":
                sp, dp = analysis.get("client_port") or 0, analysis.get("target_port") or 0
            else:
                sp, dp = analysis.get("target_port") or 0, analysis.get("client_port") or 0
                src_ip, dst_ip = dst_ip, src_ip
            pkt = self._tcp_ip(raw[:262144 - 40], src_ip, sp, dst_ip, dp,
                               analysis.get("direction"), analysis.get("conn_id", 0))
            sec = int(ts)
            usec = int((ts - sec) * 1e6)
            rec = struct.pack("<IIII", sec, usec, len(pkt), len(pkt))
            with self._lock:
                self._fh.write(rec)
                self._fh.write(pkt)
                self._fh.flush()
                self.count += 1
                self.bytes_written += len(rec) + len(pkt)
                if self.rotation > 0 and self.bytes_written >= self.rotation:
                    self._rotate()
        except Exception as e:
            if self.on_error:
                self.on_error("PCAP write: %s" % e)

    def _rotate(self):
        try:
            self._fh.close()
            folder = os.path.dirname(os.path.abspath(self.path))
            os.replace(self.path, os.path.join(folder, _ts_name("traffic", "pcap")))
            _rotate(folder, "traffic", "pcap", self.keep)
        except Exception as e:
            if self.on_error:
                self.on_error("PCAP rotate: %s" % e)
        self._open()

    def close(self):
        try:
            with self._lock:
                if self.enabled and self._fh:
                    self._fh.close()
                    self._fh = None
        except Exception:
            pass


# ---------------------------------------------------------------- CSV + TXT

class Reports:
    """CSV-отчёты по сессиям и частотности команд + текстовые логи по клиентам."""

    def __init__(self, folder, clients_folder=None, enabled=True, clients_enabled=False,
                 on_error=None):
        self.folder = folder
        self.clients_folder = clients_folder
        self.enabled = enabled
        self.clients_enabled = clients_enabled
        self.on_error = on_error
        self.closed_sessions = []
        self._client_files = {}
        self._lock = threading.Lock()
        if enabled:
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception as e:
                self.enabled = False
                if self.on_error:
                    self.on_error("CSV отключён: %s" % e)
        if clients_enabled and clients_folder:
            try:
                os.makedirs(clients_folder, exist_ok=True)
            except Exception:
                self.clients_enabled = False

    def record_closed_session(self, s):
        with self._lock:
            self.closed_sessions.append(dict(s))
            if len(self.closed_sessions) > 5000:
                self.closed_sessions = self.closed_sessions[-3000:]

    def client_log(self, analysis, line):
        if not self.clients_enabled:
            return
        try:
            ip = analysis.get("client") or "unknown"
            fn = self._client_files.get(ip)
            if fn is None or fn.closed:
                fn = open(os.path.join(self.clients_folder, "client_%s.log" % ip),
                          "a", encoding="utf-8")
                self._client_files[ip] = fn
            fn.write(line + "\n")
            fn.flush()
        except Exception as e:
            if self.on_error:
                self.on_error("ClientLog: %s" % e)

    def generate(self, hub):
        """Пишет sessions_*.csv и commands_*.csv; возвращает список путей."""
        out = []
        if not self.enabled:
            return out
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            sessions = list(self.closed_sessions) + hub.list_sessions()
            fn = os.path.join(self.folder, "sessions_%s.csv" % stamp)
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Клиент", "Порт клиента", "Канал", "Порт", "Цель",
                            "Начало", "Конец", "Пакетов TX", "Пакетов RX",
                            "Байт TX", "Байт RX", "Состояние", "Причина закрытия"])
                for s in sessions:
                    w.writerow([s.get("client"), s.get("client_port"), s.get("port_name"),
                                s.get("listen_port"),
                                "%s:%s" % (s.get("target_host"), s.get("target_port")),
                                s.get("started_iso"), s.get("end_iso", ""),
                                s.get("packets_tx", 0), s.get("packets_rx", 0),
                                s.get("bytes_tx", 0), s.get("bytes_rx", 0),
                                s.get("state"), s.get("close_reason", "")])
            out.append(fn)
        except Exception as e:
            if self.on_error:
                self.on_error("CSV sessions: %s" % e)
        try:
            fn = os.path.join(self.folder, "commands_%s.csv" % stamp)
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Команда/метод", "Количество"])
                for it in hub.top_types(1000):
                    w.writerow([it["type"], it["count"]])
                for proto, v in sorted(hub.stats_snapshot()["per_proto"].items()):
                    w.writerow(["[протокол] %s" % proto, v["packets"]])
            out.append(fn)
        except Exception as e:
            if self.on_error:
                self.on_error("CSV commands: %s" % e)
        return out

    def close(self):
        with self._lock:
            for fn in self._client_files.values():
                try:
                    fn.close()
                except Exception:
                    pass
            self._client_files = {}


# ---------------------------------------------------------------- Фасад

class ExportManager:
    def __init__(self, cfg, on_error=None, on_status=None):
        self.cfg = cfg or {}
        self.on_error = on_error or (lambda msg: None)
        self.on_status = on_status or (lambda msg: None)
        self.info = []
        storage = self.cfg.get("storage", {})
        base = storage.get("dir", "capture")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        self.jsonl = None
        self.sqlite = None
        self.pcap = None
        self.reports = None
        if storage.get("jsonl", True):
            self.jsonl = JsonlWriter(os.path.join(base, "traffic.jsonl"),
                                     storage.get("jsonl_rotation_mb", 50),
                                     storage.get("jsonl_keep", 5), self.on_error)
            self.on_status("JSONL: %s" % self.jsonl.path)
        if storage.get("sqlite", True):
            self.sqlite = SqliteWriter(storage.get("sqlite_file", os.path.join(base, "traffic.db")),
                                       retention_rows=storage.get("sqlite_retention_rows", 500000),
                                       on_error=self.on_error)
            self.on_status("SQLite: %s%s" % (storage.get("sqlite_file", os.path.join(base, "traffic.db")),
                                             "" if self.sqlite.enabled else " (ОТКЛЮЧЁН)"))
        if storage.get("pcap", True):
            self.pcap = PcapWriter(storage.get("pcap_file", os.path.join(base, "traffic.pcap")),
                                   storage.get("pcap_rotation_mb", 100),
                                   storage.get("pcap_keep", 5), self.on_error)
            self.on_status("PCAP: %s" % self.pcap.path)
        self.reports = Reports(
            storage.get("reports_dir", os.path.join(base, "reports")),
            storage.get("clients_dir", os.path.join(base, "clients")),
            enabled=storage.get("csv_reports", True),
            clients_enabled=storage.get("text_logs_per_client", False),
            on_error=self.on_error)
        if self.reports.enabled:
            self.on_status("CSV-отчёты: %s" % self.reports.folder)
        if self.reports.clients_enabled:
            self.on_status("Текстовые логи клиентов: %s" % self.reports.clients_folder)
        self.errors = 0

    def emit(self, analysis, raw, pid):
        try:
            if self.jsonl:
                self.jsonl.write(analysis)
            if self.sqlite and self.sqlite.enabled:
                self.sqlite.write_packet(analysis, pid)
            if self.pcap and self.pcap.enabled:
                self.pcap.write(analysis, raw)
            if self.reports and self.reports.clients_enabled:
                self.reports.client_log(analysis, "[%s] %s %s" % (
                    analysis.get("timestamp"), analysis.get("direction"),
                    analysis.get("summary", "")))
        except Exception:
            self.errors += 1

    def session_closed(self, s):
        if self.reports:
            self.reports.record_closed_session(s)
        if self.sqlite and self.sqlite.enabled:
            self.sqlite.write_session(s)

    def generate_reports(self, hub):
        if self.reports and self.reports.enabled:
            return self.reports.generate(hub)
        return []

    def close(self, hub=None):
        paths = []
        if hub is not None:
            try:
                paths = self.generate_reports(hub)
            except Exception:
                pass
        for sink in (self.jsonl, self.sqlite, self.pcap, self.reports):
            try:
                if sink:
                    sink.close()
            except Exception:
                pass
        return paths
