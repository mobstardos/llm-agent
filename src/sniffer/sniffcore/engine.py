# -*- coding: utf-8 -*-
"""
Прокси-движок сниффера.

На каждый настроенный порт поднимается слушатель; каждое принятое
соединение проксируется на цель с одновременным разбором трафика.
Ключевые гарантии:
  * данные РЕТРАНСЛИРУЮТСЯ всегда, даже если разбор не удался;
  * разбор — чистое наблюдение, ошибка парсера не рвёт канал;
  * автодетект протокола выполняется по первым байтам каждого направления;
  * фреймирование накапливает partial-кадры (TCP-границы не гарантированы).
"""

import socket
import threading
import time
from datetime import datetime

from .hub import Hub
from .detection import detect_best


class DirectionPump(threading.Thread):
    """Качает данные src -> dst с разбором. Отдельный поток на направление."""

    def __init__(self, engine, conn, src, dst, direction):
        super().__init__(daemon=True, name="pump-%s-%d" % (direction, conn["conn_id"]))
        self.engine = engine
        self.hub = engine.hub
        self.conn = conn
        self.src = src
        self.dst = dst
        self.direction = direction          # TX (клиент->сервер) | RX (сервер->клиент)
        self.parser = None                  # ParserAPI после автодетекта
        self.framer = None
        self.passthrough = False
        self.stop_flag = False
        self._pending = b""                 # данные до выбора парсера

    # ------------------------------------------------ луп

    def run(self):
        recv_size = self.engine.recv_size
        try:
            while not self.stop_flag:
                try:
                    data = self.src.recv(recv_size)
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    break
                if not data:
                    break
                if not self._process(data):
                    break
        finally:
            try:
                self.engine.close_conn(self.conn["conn_id"], "направление %s закрыто" % self.direction)
            except Exception:
                pass

    # ------------------------------------------------ обработка чанка

    def _process(self, data):
        try:
            self.dst.sendall(data)          # ПЕРЕДАЧА ВАЖНЕЕ РАЗБОРА
        except OSError:
            return False

        if self.passthrough:
            self._emit(self._raw_analysis(data, "прозрачный режим"), data)
            return True

        if self.parser is None:
            self._pending += data
            if not self._detect_pending():
                return True                 # ждём больше данных
            data = self._pending            # разбор начинается с самого первого байта потока
            self._pending = b""

        if self.framer is not None:
            try:
                frames = self.framer.feed(data)
            except Exception as e:
                self.engine.console.info(
                    "%s: фреймер отклонил поток (%s), канал переведён в прозрачный режим"
                    % (self.conn["port_name"], e))
                self._passthrough_on()
                self._emit(self._raw_analysis(data, "фреймер отклонил поток"), data)
                return True
            for frame in frames:
                self._emit_frame(frame)
        else:
            # chunk-mode парсер (hexdump/JSON может сам копить — нет, JSON копит;
            # chunk-mode только у тех, у кого create_framer -> None)
            self._emit_frame(data)
        return True

    # ------------------------------------------------ автодетект

    MIN_DETECT = 8
    MAX_PENDING = 65536

    def _detect_pending(self):
        """True — парсер выбран (или прозрачный режим), False — ждать данных."""
        if len(self._pending) < self.MIN_DETECT:
            return False
        best, confidence = detect_best(self.engine.registry, self._pending, self.direction)
        if best is None:
            if len(self._pending) < self.MAX_PENDING:
                return False
            best = self.engine.registry.get(self.engine.fallback_parser)
            confidence = 0
        if best is None:
            self._passthrough_on()
            return True
        self.parser = best
        try:
            self.framer = best.create_framer(self.conn) if best.create_framer else None
        except Exception:
            self.framer = None
        self.conn["protocol_%s" % self.direction.lower()] = best.name
        if self.direction == "TX":
            self.engine.console.detected({"port_name": self.conn["port_name"],
                                          "protocol": best.label, "confidence": confidence})
            self.hub.set_detected(self.conn["port_name"], best.name, confidence)
        return True

    def _passthrough_on(self):
        self.passthrough = True
        self.framer = None
        self.parser = None

    # ------------------------------------------------ разбор кадра + события

    def _emit_frame(self, frame):
        a = self._analyze(frame)
        self._emit(a, frame)

    def _analyze(self, frame):
        ctx = dict(self.conn)
        ctx["direction"] = self.direction
        if self.parser is None:
            return self._raw_analysis(frame, "нет парсера")
        try:
            a = self.parser.parse(frame, ctx)
            if not isinstance(a, dict):
                a = {"protocol": self.parser.name.upper(), "valid": False,
                     "notes": "парсер вернул не dict"}
            a.setdefault("protocol", self.parser.name.upper())
        except Exception as e:
            a = {"protocol": (self.parser.name.upper() if self.parser else "RAW"),
                 "valid": False, "parse_error": str(e)[:200]}
        # подмешиваем контекст (парсер мог вернуть только поля протокола)
        a.setdefault("summary", "")
        return a

    def _raw_analysis(self, data, note):
        fb = self.engine.registry.get(self.engine.fallback_parser)
        if fb:
            try:
                a = fb.parse(data[:8192], dict(self.conn))
                a["protocol"] = "RAW"
                a.setdefault("summary", "прозрачный режим")
                a["notes"] = note
                return a
            except Exception:
                pass
        return {"protocol": "RAW", "valid": True, "summary": "прозрачный режим",
                "notes": note}

    def _emit(self, analysis, raw):
        try:
            a = dict(analysis)
            a["direction"] = self.direction
            a["client"] = self.conn.get("client")
            a["client_port"] = self.conn.get("client_port")
            a["port_name"] = self.conn.get("port_name")
            a["listen_port"] = self.conn.get("listen_port")
            a["target_host"] = self.conn.get("target_host")
            a["target_port"] = self.conn.get("target_port")
            a["conn_id"] = self.conn.get("conn_id")
            a["ts"] = time.time()
            a["timestamp"] = datetime.now().isoformat(timespec="milliseconds")
            if raw is not None:
                a["size"] = len(raw)
            else:
                a["size"] = 0
            self.engine.emit(a, raw)
        except Exception:
            pass


class ConnectionHandler:
    """Одно проксируемое соединение: клиент <-> сниффер <-> сервер."""

    def __init__(self, engine, client_sock, client_addr, port_cfg):
        self.engine = engine
        self.client_sock = client_sock
        self.server_sock = None
        self.client_addr = client_addr
        self.port_cfg = port_cfg
        self.conn_id = engine.next_conn_id()
        self.closed = False
        self.close_reason = ""
        target = port_cfg["target_host"], port_cfg["target_port"]
        self.conn = {
            "conn_id": self.conn_id,
            "client": client_addr[0],
            "client_port": client_addr[1],
            "listen_port": port_cfg["listen"],
            "port_name": port_cfg["name"],
            "target_host": port_cfg["target_host"],
            "target_port": port_cfg["target_port"],
            "started_ts": time.time(),
            "started_iso": datetime.now().isoformat(timespec="seconds"),
            "state": "connecting",
            "packets_tx": 0, "packets_rx": 0, "bytes_tx": 0, "bytes_rx": 0,
        }

    def run(self):
        engine = self.engine
        try:
            self.client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_sock.settimeout(engine.connect_timeout)
            server_sock.connect((self.port_cfg["target_host"], self.port_cfg["target_port"]))
            server_sock.settimeout(None)
            server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception as e:
            engine.console.connect({"ok": False, "error": str(e), **self.conn})
            self.conn["state"] = "failed"
            engine.hub.session_open(self.conn)
            engine.hub.session_close(self.conn_id, "цель недоступна: %s" % e)
            try:
                self.client_sock.close()
            except Exception:
                pass
            engine.forget_conn(self.conn_id)
            return

        try:
            self.client_sock.settimeout(None)
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        self.server_sock = server_sock

        self.conn["state"] = "active"
        engine.hub.session_open(self.conn)
        engine.alerts.note_session_open(self.conn)
        engine.console.connect({"ok": True, **self.conn})

        t_tx = DirectionPump(engine, self.conn, self.client_sock, server_sock, "TX")
        t_rx = DirectionPump(engine, self.conn, server_sock, self.client_sock, "RX")
        engine.register_pumps(self.conn_id, t_tx, t_rx)
        t_tx.start()
        t_rx.start()
        while t_tx.is_alive() and t_rx.is_alive():
            if engine.stopping.is_set():
                break
            t_tx.join(timeout=0.5)
            t_rx.join(timeout=0.5)
        if not self.closed:
            reason = engine.drain_reason(self.conn_id) or "клиент или сервер закрыл соединение"
            self._close(reason)
        engine.forget_conn(self.conn_id)

    def _close(self, reason):
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self.conn["state"] = "closed"
        self.conn["close_reason"] = reason
        self.conn["end_iso"] = datetime.now().isoformat(timespec="seconds")
        s = self.engine.hub.session_close(self.conn_id, reason)
        if s:
            self.engine.exports.session_closed(s)
        self.engine.alerts.note_session_closed(self.conn_id)
        self.engine.console.disconnect(self.conn)
        for sock in (self.client_sock, self.server_sock):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


class Engine:
    """Порт-слушатели + реестр активных соединений."""

    def __init__(self, cfg, hub, console, exports, alerts, registry):
        self.cfg = cfg
        self.hub = hub
        self.console = console
        self.exports = exports
        self.alerts = alerts
        self.registry = registry
        self.stopping = threading.Event()
        self.listeners = []
        self.conn_lock = threading.Lock()
        self.conns = {}           # conn_id -> ConnectionHandler
        self.pumps = {}           # conn_id -> (pump_tx, pump_rx)
        self.reasons = {}         # conn_id -> строка причины
        self._conn_seq = 0
        self.recv_size = int(cfg.get("buffers", {}).get("recv_size", 65536))
        self.connect_timeout = float(cfg.get("buffers", {}).get("connect_timeout", 10))
        self.fallback_parser = cfg.get("detection", {}).get("fallback", "hexdump")

    # ---------- служебное ----------

    def next_conn_id(self):
        with self.conn_lock:
            self._conn_seq += 1
            return self._conn_seq

    def register_pumps(self, conn_id, tx, rx):
        with self.conn_lock:
            self.pumps[conn_id] = (tx, rx)

    def close_conn(self, conn_id, reason):
        with self.conn_lock:
            self.reasons[conn_id] = reason
            handler = self.conns.get(conn_id)
        if handler and not handler.closed:
            handler._close(reason)

    def drain_reason(self, conn_id):
        with self.conn_lock:
            return self.reasons.pop(conn_id, None)

    def forget_conn(self, conn_id):
        with self.conn_lock:
            self.conns.pop(conn_id, None)
            self.pumps.pop(conn_id, None)
            self.reasons.pop(conn_id, None)

    def emit(self, analysis, raw):
        """Единая точка обработки разобранного пакета."""
        entry = self.hub.add_packet(analysis, raw)
        analysis["_pid"] = entry["id"]
        conn = self.conns.get(analysis.get("conn_id"))
        if conn is not None:
            if analysis["direction"] == "TX":
                conn.conn["packets_tx"] += 1
                conn.conn["bytes_tx"] += analysis.get("size", 0)
            else:
                conn.conn["packets_rx"] += 1
                conn.conn["bytes_rx"] += analysis.get("size", 0)
        self.console.packet(analysis)
        self.exports.emit(analysis, raw, entry["id"])
        self.alerts.check_packet(analysis, analysis.get("port_name"))

    # ---------- запуск ----------

    def start(self):
        for port_cfg in self.cfg.get("ports", []):
            t = threading.Thread(target=self._listen_loop, args=(port_cfg,),
                                 daemon=True, name="listener-%d" % port_cfg["listen"])
            self.listeners.append(t)
            t.start()

    def _listen_loop(self, port_cfg):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host = self.cfg.get("listen_host", "0.0.0.0")
        try:
            srv.bind((host, port_cfg["listen"]))
            srv.listen(64)
        except Exception as e:
            self.console.error("Порт %s (%s): %s" % (port_cfg["listen"], port_cfg["name"], e))
            return
        port_cfg["_socket"] = srv
        self.console.info("Слушаю %s:%s -> %s:%s [%s]" % (
            host, port_cfg["listen"], port_cfg["target_host"], port_cfg["target_port"],
            port_cfg["name"]))
        while not self.stopping.is_set():
            try:
                srv.settimeout(0.5)
                try:
                    client, addr = srv.accept()
                except socket.timeout:
                    continue
                handler = ConnectionHandler(self, client, addr, port_cfg)
                with self.conn_lock:
                    self.conns[handler.conn_id] = handler
                threading.Thread(target=handler.run, daemon=True,
                                 name="conn-%d" % handler.conn_id).start()
            except OSError:
                break
            except Exception as e:
                self.console.error("accept: %s" % e)
                time.sleep(0.2)

    def stop(self):
        self.stopping.set()
        for port_cfg in self.cfg.get("ports", []):
            srv = port_cfg.get("_socket")
            if srv:
                try:
                    srv.close()
                except Exception:
                    pass
        with self.conn_lock:
            handlers = list(self.conns.values())
            pumps = list(self.pumps.values())
        for tx, rx in pumps:
            tx.stop_flag = True
            rx.stop_flag = True
        for h in handlers:
            try:
                h.client_sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        deadline = time.time() + 3
        for h in handlers:
            while not h.closed and time.time() < deadline:
                time.sleep(0.05)
        self.alerts.close()
