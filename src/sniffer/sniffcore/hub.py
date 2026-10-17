# -*- coding: utf-8 -*-
"""
Hub — центральный узел событий сниффера.

Хранит: кольцевой буфер пакетов, реестр сессий, агрегированную статистику,
временные ряды для графиков; рассылает события подписчикам (SSE-клиенты
веб-панели). Потокобезопасен.
"""

import threading
import time
from collections import deque
from datetime import datetime

RING_MAX = 5000            # сколько последних пакетов держим в памяти
RAW_CAP = 64 * 1024        # максимум сырого payload, хранимого для просмотра
SERIES_MAX = 900           # точек секундного графика (15 минут)

SUMMARY_KEYS = ("cmd_type", "cmd_class", "cmd", "session", "txid",
                "msg_type", "method", "method_type", "seq_id",
                "http_method", "http_path", "http_code", "http_version",
                "json", "json_keys", "json_method", "json_id", "json_items",
                "body_size", "unit_id", "function", "address", "quantity",
                "values", "byte_count", "write_address", "write_quantity",
                "exception", "tables", "strings", "fields", "frame_len",
                "decompressed_size", "version", "payload_len")


class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.packets = deque(maxlen=RING_MAX)
        self.index = {}                       # id -> запись пакета
        self.next_id = 1
        self.sessions = {}                    # conn_id -> dict
        self.subscribers = []                 # list[queue.Queue]
        self.start_time = time.time()
        self.dropped_events = 0
        self.alerts = deque(maxlen=300)
        # агрегаты
        self.totals = {"packets": 0, "bytes": 0, "tx_packets": 0, "rx_packets": 0,
                       "tx_bytes": 0, "rx_bytes": 0, "connections": 0,
                       "errors": 0, "parse_errors": 0}
        self.per_port = {}    # name -> {packets, bytes, conns}
        self.per_proto = {}   # protocol -> {packets, bytes}
        self.per_type = {}    # cmd_type/method_type -> count
        self.per_client = {}  # ip -> {packets, bytes}
        self.series = deque(maxlen=SERIES_MAX)   # (ts, pps, bps)
        self._window_packets = 0
        self._window_bytes = 0
        self.detected_info = {}  # port_name -> {protocol, confidence}

    # ---------- подписчики (SSE) ----------

    def subscribe(self):
        q = deque(maxlen=512)
        item = {"q": q, "lock": threading.Lock(), "alive": True}
        with self.lock:
            self.subscribers.append(item)
        return item

    def unsubscribe(self, item):
        item["alive"] = False
        with self.lock:
            if item in self.subscribers:
                self.subscribers.remove(item)

    def broadcast(self, event, data):
        payload = (event, data)
        with self.lock:
            dead = []
            for sub in self.subscribers:
                try:
                    sub["q"].append(payload)
                except Exception:
                    dead.append(sub)
            for sub in dead:
                self.subscribers.remove(sub)

    def drain(self, item, timeout=1.0):
        """Берёт очередное событие подписчика (с ожиданием) или None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not item["alive"]:
                return None
            with self.lock:
                if item["q"]:
                    return item["q"].popleft()
            time.sleep(0.05)
        with self.lock:
            if item["q"]:
                return item["q"].popleft()
        return None

    # ---------- пакеты ----------

    def add_packet(self, analysis, raw):
        """Регистрирует разобранный пакет: кольцо, индекс, SSE, ряды."""
        pid = 0
        with self.lock:
            pid = self.next_id
            self.next_id += 1
            self.totals["packets"] += 1
            self.totals["bytes"] += analysis.get("size", 0)
            if analysis.get("direction") == "TX":
                self.totals["tx_packets"] += 1
                self.totals["tx_bytes"] += analysis.get("size", 0)
            else:
                self.totals["rx_packets"] += 1
                self.totals["rx_bytes"] += analysis.get("size", 0)
            if analysis.get("parse_error"):
                self.totals["parse_errors"] += 1

            port_name = analysis.get("port_name", "?")
            pp = self.per_port.setdefault(port_name, {"packets": 0, "bytes": 0, "conns": 0})
            pp["packets"] += 1
            pp["bytes"] += analysis.get("size", 0)
            proto = analysis.get("protocol", "RAW")
            pr = self.per_proto.setdefault(proto, {"packets": 0, "bytes": 0})
            pr["packets"] += 1
            pr["bytes"] += analysis.get("size", 0)
            ptype = analysis.get("cmd_type") or analysis.get("method_type")
            if ptype:
                self.per_type[ptype] = self.per_type.get(ptype, 0) + 1
            cl = analysis.get("client", "?")
            pc = self.per_client.setdefault(cl, {"packets": 0, "bytes": 0})
            pc["packets"] += 1
            pc["bytes"] += analysis.get("size", 0)
            self._window_packets += 1
            self._window_bytes += analysis.get("size", 0)

        entry = {
            "id": pid,
            "ts": analysis.get("ts", time.time()),
            "ts_iso": analysis.get("timestamp"),
            "port_name": port_name,
            "listen_port": analysis.get("listen_port"),
            "client": analysis.get("client"),
            "client_port": analysis.get("client_port"),
            "direction": analysis.get("direction"),
            "protocol": proto,
            "valid": bool(analysis.get("valid")),
            "size": analysis.get("size", 0),
            "summary": analysis.get("summary", ""),
            "info": {k: analysis[k] for k in SUMMARY_KEYS if k in analysis and analysis[k] is not None},
            "notes": analysis.get("notes"),
        }
        raw_stored = None
        if raw is not None:
            raw_stored = raw[:RAW_CAP]
        with self.lock:
            self.packets.append(entry)
            self.index[pid] = (entry, raw_stored)
            if len(self.index) > RING_MAX * 2:
                min_keep = min(self.index) if self.index else 0
                # подчистка: убираем id, которых уже нет в кольце
                live = {p["id"] for p in self.packets}
                for k in list(self.index.keys()):
                    if k not in live:
                        del self.index[k]
        self.broadcast("packet", entry)
        return entry

    def get_packet(self, pid):
        with self.lock:
            item = self.index.get(pid)
            return item if item else (None, None)

    def recent_packets(self, limit=300):
        with self.lock:
            items = list(self.packets)[-limit:]
            items.reverse()
            return items

    # ---------- сессии ----------

    def session_open(self, conn):
        with self.lock:
            self.totals["connections"] += 1
            self.sessions[conn["conn_id"]] = dict(conn)
            pp = self.per_port.setdefault(conn["port_name"], {"packets": 0, "bytes": 0, "conns": 0})
            pp["conns"] += 1
        self.broadcast("session", {"action": "open", "session": dict(conn)})

    def session_update(self, conn_id, **fields):
        with self.lock:
            s = self.sessions.get(conn_id)
            if s:
                s.update(fields)

    def session_close(self, conn_id, reason=""):
        with self.lock:
            s = self.sessions.pop(conn_id, None)
        if s:
            s["state"] = "closed"
            s["end_iso"] = datetime.now().isoformat(timespec="seconds")
            s["close_reason"] = reason
            self.broadcast("session", {"action": "close", "session": s})
        return s

    def list_sessions(self):
        with self.lock:
            return [dict(s) for s in self.sessions.values()]

    # ---------- серия для графика ----------

    def tick_series(self):
        with self.lock:
            self.series.append((time.time(), self._window_packets, self._window_bytes))
            self._window_packets = 0
            self._window_bytes = 0

    # ---------- снапшоты ----------

    def top_types(self, n=12):
        with self.lock:
            items = sorted(self.per_type.items(), key=lambda kv: kv[1], reverse=True)[:n]
            return [{"type": k, "count": v} for k, v in items]

    def top_clients(self, n=10):
        with self.lock:
            items = sorted(self.per_client.items(), key=lambda kv: kv[1]["packets"], reverse=True)[:n]
            return [{"client": k, "packets": v["packets"], "bytes": v["bytes"]} for k, v in items]

    def stats_snapshot(self):
        with self.lock:
            now = time.time()
            pps = self.series[-1][1] if self.series else 0
            return {
                "uptime_sec": round(now - self.start_time, 1),
                "totals": dict(self.totals),
                "per_port": {k: dict(v) for k, v in self.per_port.items()},
                "per_proto": {k: dict(v) for k, v in self.per_proto.items()},
                "per_type": dict(self.per_type),
                "top_types": self.top_types(),
                "top_clients": self.top_clients(),
                "sessions_active": len(self.sessions),
                "pps": pps,
                "subscribers": len(self.subscribers),
                "dropped_events": self.dropped_events,
                "detected": dict(self.detected_info),
            }

    def series_snapshot(self, limit=180):
        with self.lock:
            return list(self.series)[-limit:]

    def add_alert(self, alert):
        with self.lock:
            self.alerts.append(alert)
        self.broadcast("alert", alert)

    def list_alerts(self, limit=100):
        with self.lock:
            items = list(self.alerts)[-limit:]
            items.reverse()
            return items

    def set_detected(self, port_name, protocol, confidence):
        """Запоминает автодетект канала; уверенность ниже уже записанной
        не понижает статус (низкокачественный детект не затирает хороший)."""
        with self.lock:
            cur = self.detected_info.get(port_name)
            if cur and int(cur.get("confidence", 0)) >= int(confidence):
                return
            self.detected_info[port_name] = {"protocol": protocol, "confidence": confidence}
        self.broadcast("detected", {"port_name": port_name, "protocol": protocol,
                                    "confidence": confidence})
