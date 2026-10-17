# -*- coding: utf-8 -*-
"""
Движок тревог: настраиваемые правила на полях разобранного пакета,
пороги "N раз за M секунд", кулдаун, webhook, детектор молчания клиентов.

Правило (config.alerts.rules):
  {
    "name": "Откат безнала",
    "severity": "crit",                       # info | warn | crit
    "match": {"protocol": "THRIFT", "method_type": "CASHLESS_ROLLBACK"},
    "size_gt": 1000000, "size_lt": null,      # опционально, размер пакета
    "keyword": "error",                        # подстрока в summary/строках
    "threshold": {"count": 3, "window_sec": 60},  # опционально
    "actions": ["console", "web", "sound", "webhook", "file"]
  }
Совпадение = все непустые поля "match" совпали (AND). Правила проверяются по очереди.
"silence_sec" в alerts — отдельная проверка молчания активных сессий.
"""

import json
import os
import threading
import time
import urllib.request
from collections import defaultdict, deque

VALID_ACTIONS = ("console", "web", "sound", "webhook", "file")


class AlertEngine:
    def __init__(self, cfg, console, hub, on_status=None):
        cfg = cfg or {}
        self.console = console
        self.hub = hub
        self.rules = []
        self.webhook_url = cfg.get("webhook_url") or ""
        self.cooldown = float(cfg.get("cooldown_sec", 30))
        self.silence_sec = float(cfg.get("silence_sec", 0) or 0)
        self.alerts_file = cfg.get("alerts_file", os.path.join("capture", "alerts.jsonl"))
        self.on_status = on_status or (lambda m: None)
        self.fired_total = 0
        self._lock = threading.Lock()
        self._rule_windows = defaultdict(lambda: deque(maxlen=500))   # rule -> [ts]
        self._rule_last = {}                                          # rule -> last fired ts
        self._last_activity = {}                                      # conn_id -> ts
        self._silence_fired = set()
        self._stop = threading.Event()
        self._load_rules(cfg.get("rules", []))
        if self.silence_sec > 0:
            t = threading.Thread(target=self._silence_loop, daemon=True, name="silence-watch")
            t.start()

    # ---------- конфигурация ----------

    def _load_rules(self, rules):
        loaded = 0
        for r in rules or []:
            try:
                if not r.get("name"):
                    continue
                self.rules.append({
                    "name": r["name"],
                    "severity": r.get("severity", "warn"),
                    "match": dict(r.get("match") or {}),
                    "size_gt": r.get("size_gt"),
                    "size_lt": r.get("size_lt"),
                    "keyword": (r.get("keyword") or "").lower() or None,
                    "threshold": r.get("threshold") or None,
                    "actions": [a for a in (r.get("actions") or ["console", "web", "sound"])
                                if a in VALID_ACTIONS],
                })
                loaded += 1
            except Exception:
                continue
        self.on_status("Тревоги: загружено правил — %d" % loaded)

    def reload_config(self, alerts_cfg):
        """Горячая перезагрузка правил без остановки сниффера."""
        alerts_cfg = alerts_cfg or {}
        with self._lock:
            self.rules = []
            self.webhook_url = alerts_cfg.get("webhook_url") or ""
            self.cooldown = float(alerts_cfg.get("cooldown_sec", 30))
            self.silence_sec = float(alerts_cfg.get("silence_sec", 0) or 0)
            self._load_rules(alerts_cfg.get("rules", []))

    # ---------- проверка пакета ----------

    def check_packet(self, analysis, port_name):
        try:
            self._note_activity(analysis)
            if not self.rules:
                return
            haystack = " ".join([
                str(analysis.get("summary", "")),
                " ".join(map(str, analysis.get("strings") or [])),
                str(analysis.get("method") or ""),
                str(analysis.get("http_path") or ""),
            ]).lower()
            for rule in self.rules:
                if not self._match(rule, analysis, haystack, port_name):
                    continue
                if not self._threshold_ok(rule):
                    continue
                self._fire(rule, analysis)
        except Exception:
            pass

    def _match(self, rule, a, haystack, port_name):
        for key, want in rule["match"].items():
            got = a.get(key)
            if key == "port_name":
                got = port_name
            if isinstance(want, (list, tuple, set)):
                if str(got) not in {str(x) for x in want}:
                    return False
            else:
                if str(got or "") != str(want):
                    return False
        size = a.get("size", 0) or 0
        if rule["size_gt"] is not None and size <= rule["size_gt"]:
            return False
        if rule["size_lt"] is not None and size >= rule["size_lt"]:
            return False
        if rule["keyword"] and rule["keyword"] not in haystack:
            return False
        return True

    def _threshold_ok(self, rule):
        thr = rule["threshold"]
        if not thr or not thr.get("count"):
            return True
        now = time.time()
        win = float(thr.get("window_sec", 60))
        with self._lock:
            dq = self._rule_windows[rule["name"]]
            dq.append(now)
            while dq and now - dq[0] > win:
                dq.popleft()
            return len(dq) >= int(thr["count"])

    def _fire(self, rule, a):
        now = time.time()
        with self._lock:
            last = self._rule_last.get(rule["name"], 0)
            if now - last < self.cooldown:
                return
            self._rule_last[rule["name"]] = now
        self.fired_total += 1
        alert = {
            "id": self.fired_total,
            "ts": now,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rule": rule["name"],
            "severity": rule["severity"],
            "actions": rule["actions"],
            "detail": a.get("summary", ""),
            "protocol": a.get("protocol"),
            "client": a.get("client"),
            "port_name": a.get("port_name"),
            "packet_id": a.get("_pid"),
        }
        if "console" in rule["actions"]:
            self.console.alert(alert)
        if "web" in rule["actions"] or "sound" in rule["actions"]:
            alert["sound"] = "sound" in rule["actions"]
            self.hub.add_alert(alert)
        if "file" in rule["actions"]:
            self._write_file(alert, a)
        if "webhook" in rule["actions"] and self.webhook_url:
            threading.Thread(target=self._webhook, args=(alert,), daemon=True).start()

    def _write_file(self, alert, a):
        try:
            d = os.path.dirname(os.path.abspath(self.alerts_file))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            rec = dict(alert)
            rec["packet"] = {k: a.get(k) for k in ("protocol", "client", "size", "summary")}
            with open(self.alerts_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _webhook(self, alert):
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps({"type": "sniffer_alert", "alert": alert},
                                ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req, timeout=5).read(1024)
        except Exception as e:
            self.console.error("Webhook не доставлен (%s): %s" % (alert["rule"], e))

    # ---------- молчание ----------

    def _note_activity(self, a):
        cid = a.get("conn_id")
        if cid is not None:
            self._last_activity[cid] = time.time()
            self._silence_fired.discard(cid)

    def note_session_open(self, conn):
        self._last_activity[conn["conn_id"]] = time.time()

    def note_session_closed(self, conn_id):
        self._last_activity.pop(conn_id, None)
        self._silence_fired.discard(conn_id)

    def _silence_loop(self):
        while not self._stop.is_set():
            try:
                now = time.time()
                sessions = self.hub.list_sessions()
                for s in sessions:
                    cid = s.get("conn_id")
                    last = self._last_activity.get(cid, s.get("started_ts", now))
                    if now - last >= self.silence_sec and cid not in self._silence_fired:
                        self._silence_fired.add(cid)
                        alert = {
                            "id": 0, "ts": now,
                            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "rule": "Молчание клиента",
                            "severity": "warn", "actions": ["console", "web"],
                            "detail": "%s нет данных более %d с (%s)" % (
                                s.get("client"), int(now - last), s.get("port_name")),
                            "client": s.get("client"), "port_name": s.get("port_name"),
                            "sound": True,
                        }
                        self.console.alert(alert)
                        self.hub.add_alert(alert)
            except Exception:
                pass
            self._stop.wait(10.0)

    def close(self):
        self._stop.set()
