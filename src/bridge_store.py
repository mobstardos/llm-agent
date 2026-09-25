"""BridgeStore — файловый мост «расширение браузера ↔ агенты».

Расширение — тонкий клиент: вся логика живёт на локальном сервере,
а обмен идёт через JSON-файлы в data/bridge/:

    tabs.json      — последний снапшот открытых вкладок (от расширения);
    jobs.json      — очередь задач сервер → расширение (прочитай вкладку);
    captures.jsonl — журнал захватов расширение → сервер (страницы/выделения).

Почему файлы, а не БД: BridgeStore используют ДВА процесса (uvicorn с
API-роутером фичи и stdio-MCP «bridge»), для маленьких объёмов
(вкладки ≤ десятков, задачи десятки, захваты ≤ CAPTURES_MAX) файлы
проще и прозрачно диагностируются.

Гарантии целостности:
  * каждая запись — атомарная: tmp-файл + os.replace();
  * in-process — threading.RLock;
  * cross-process (uvicorn ↔ MCP) — O_EXCL lock-файл с таймаутом и
    сломом «зависших» замков (старше LOCK_STALE_S);
  * каждая операция читает свежий файл (read-modify-write), поэтому
    данные не теряются между процессами.

Служебные правила:
  * задача живёт JOB_TTL_S секунд, потом снимается (expired);
  * задача в status=dispatched дольше JOB_REQUEUE_AFTER_S считается
    «потерянной» (воркер уснул/выключили браузер) — автоматически
    возвращается в pending (requeue);
  * захватов хранится не больше CAPTURES_MAX (старые вытесняются);
  * wait_job() — асинхронное ожидание результата задачи (API-процесс).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Константы эксплуатации ─────────────────────────────────────
CAPTURES_MAX = 500          # сколько захватов храним (вытеснение старых)
JOB_TTL_S = 600             # жизнь задачи с момента создания, сек
JOB_REQUEUE_AFTER_S = 90    # dispatched без результата → снова pending
JOB_WAIT_DEFAULT_S = 60     # дефолтный таймаут wait_job()
LOCK_STALE_S = 5            # lock-файл старше этого — ломаем
CAPTURE_TEXT_MAX = 40000    # страховка размера текста захвата


def _now() -> float:
    return time.time()


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


class BridgeStore:
    """Общее хранилище моста: вкладки / задачи / захваты."""

    def __init__(self, base_dir: str | Path):
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tabs_path = self.dir / "tabs.json"
        self.jobs_path = self.dir / "jobs.json"
        self.captures_path = self.dir / "captures.jsonl"
        self.lock_path = self.dir / ".bridge.lock"
        self._rl = threading.RLock()

    # ═══════════════════════════════════════════════════════
    # Низкий уровень: атомарная запись + межпроцессный замок
    # ═══════════════════════════════════════════════════════
    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except Exception:
            # битый/полузаписанный файл — не роняем мост
            logger.warning("BridgeStore: не читается %s — считаю пустым",
                           path.name)
            return default

    def _write_json_atomic(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)

    def _append_jsonl(self, path: Path, obj: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict]:
        out: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except FileNotFoundError:
            pass
        return out

    def _write_jsonl_atomic(self, path: Path, items: list[dict]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    class _FileLock:
        """Межпроцессный O_EXCL-замок с ожиданием и сломом зависших."""

        def __init__(self, path: Path, stale_s: float, timeout: float = 10.0):
            self.path = path
            self.stale_s = stale_s
            self.timeout = timeout
            self._fd: int | None = None

        def __enter__(self):
            deadline = _now() + self.timeout
            while True:
                try:
                    self._fd = os.open(str(self.path),
                                       os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(self._fd, str(os.getpid()).encode())
                    return self
                except FileExistsError:
                    try:
                        age = _now() - self.path.stat().st_mtime
                        if age > self.stale_s:
                            os.unlink(self.path)   # слом зависшего замка
                            continue
                    except FileNotFoundError:
                        continue
                    if _now() > deadline:
                        # не могли получить — работаем без замка
                        # (атомарный replace всё равно спасает данные)
                        self._fd = None
                        return self
                    time.sleep(0.02)
                except OSError:
                    self._fd = None
                    return self

        def __exit__(self, *exc):
            if self._fd is not None:
                try:
                    os.close(self._fd)
                    os.unlink(self.path)
                except OSError:
                    pass
            return False

    def _locked(self):
        return BridgeStore._FileLock(self.lock_path, LOCK_STALE_S)

    # ═══════════════════════════════════════════════════════
    # Вкладки (снапшот от расширения)
    # ═══════════════════════════════════════════════════════
    def register_tabs(self, tabs: list[dict], meta: dict | None = None) -> dict:
        """Расширение присылает полный снапшот открытых вкладок."""
        clean: list[dict] = []
        for t in tabs or []:
            if not isinstance(t, dict):
                continue
            clean.append({
                "id": str(t.get("id", "")),
                "title": str(t.get("title", ""))[:300],
                "url": str(t.get("url", ""))[:2000],
                "active": bool(t.get("active")),
                "window": int(t.get("window", 0) or 0),
            })
        snap = {"ts": _now(), "meta": meta or {}, "tabs": clean}
        with self._rl, self._locked():
            self._write_json_atomic(self.tabs_path, snap)
        return {"count": len(clean), "ts": snap["ts"]}

    def list_tabs(self) -> dict:
        with self._rl:
            snap = self._read_json(self.tabs_path, {"ts": 0, "tabs": []})
        age = _now() - float(snap.get("ts") or 0)
        return {"ts": snap.get("ts", 0), "age_s": round(age, 1),
                "count": len(snap.get("tabs", [])),
                "tabs": snap.get("tabs", [])}

    # ═══════════════════════════════════════════════════════
    # Задачи сервер → расширение
    # ═══════════════════════════════════════════════════════
    def _maintenance(self, jobs: list[dict]) -> list[dict]:
        """TTL + requeue «потерянных» dispatched. Вызывать под замком."""
        now = _now()
        out: list[dict] = []
        for j in jobs:
            age = now - float(j.get("created_at") or 0)
            if j.get("status") in ("done", "failed", "expired"):
                if age <= JOB_TTL_S * 2:      # результат держим немного
                    out.append(j)
                continue
            if age > JOB_TTL_S:
                j["status"] = "expired"
                j["finished_at"] = now
                out.append(j)
                continue
            if (j.get("status") == "dispatched"
                    and now - float(j.get("dispatched_at") or 0)
                    > JOB_REQUEUE_AFTER_S):
                j["status"] = "pending"
                j["dispatched_at"] = 0
                j["requeues"] = int(j.get("requeues", 0)) + 1
            out.append(j)
        return out

    def _load_jobs_maintained(self) -> list[dict]:
        """Читает jobs.json, прогоняет maintenance и, если что-то
        изменилось (requeue/expire/вычищены старые), сохраняет файл.
        Без этого wait_job()/get_job() не увидели бы requeue и TTL —
        статус «завис» бы только в списке, но не в ожидании."""
        jobs = self._read_json(self.jobs_path, [])
        fixed = self._maintenance(jobs)
        if (len(fixed) != len(jobs) or
                any(a.get("status") != b.get("status")
                    or a.get("requeues") != b.get("requeues")
                    for a, b in zip(fixed, jobs))):
            self._write_json_atomic(self.jobs_path, fixed)
        return fixed

    def put_job(self, kind: str, payload: dict | None = None,
                source: str = "api") -> dict:
        """Создать задачу для расширения (прочитай вкладку, сделай…)."""
        job = {
            "id": _short_id(),
            "kind": kind,
            "payload": payload or {},
            "source": source,
            "status": "pending",
            "created_at": _now(),
            "dispatched_at": 0,
            "finished_at": 0,
            "requeues": 0,
            "result": None,
            "error": None,
        }
        with self._rl, self._locked():
            jobs = self._read_json(self.jobs_path, [])
            jobs = self._maintenance(jobs)
            jobs.append(job)
            self._write_json_atomic(self.jobs_path, jobs)
        return job

    def pull_jobs(self, limit: int = 10) -> list[dict]:
        """Расширение забирает pending-задачи (long-poll уже отработал)."""
        with self._rl, self._locked():
            jobs = self._read_json(self.jobs_path, [])
            jobs = self._maintenance(jobs)
            taken: list[dict] = []
            now = _now()
            for j in jobs:
                if j.get("status") == "pending" and len(taken) < limit:
                    j["status"] = "dispatched"
                    j["dispatched_at"] = now
                    taken.append({
                        "id": j["id"], "kind": j["kind"],
                        "payload": j.get("payload", {}),
                        "source": j.get("source", ""),
                    })
            if taken:
                self._write_json_atomic(self.jobs_path, jobs)
            return taken

    def ack_job(self, job_id: str) -> bool:
        """Расширение подтвердило приём (диагностика dispatched→acked)."""
        return self._update_job(job_id, lambda j: None, to_status="acked")

    def complete_job(self, job_id: str, result: Any,
                     error: str | None = None) -> bool:
        status = "failed" if error else "done"
        def mut(j: dict):
            j["result"] = None if error else _clip_result(result)
            j["error"] = error
        return self._update_job(job_id, mut, to_status=status,
                                finished=True)

    def _update_job(self, job_id: str, mut, to_status: str,
                    finished: bool = False) -> bool:
        found = False
        with self._rl, self._locked():
            jobs = self._read_json(self.jobs_path, [])
            jobs = self._maintenance(jobs)
            for j in jobs:
                if j.get("id") != job_id:
                    continue
                if j.get("status") in ("done", "failed", "expired"):
                    break
                mut(j)
                j["status"] = to_status
                if finished:
                    j["finished_at"] = _now()
                found = True
                break
            if found:
                self._write_json_atomic(self.jobs_path, jobs)
        return found

    async def wait_job(self, job_id: str,
                       timeout_s: float = JOB_WAIT_DEFAULT_S,
                       poll_s: float = 0.4) -> dict:
        """Асинхронное ожидание завершения задачи (агент/API ждёт ответ)."""
        deadline = _now() + timeout_s
        while _now() < deadline:
            job = self.get_job(job_id)
            if job is None:
                return {"ok": False, "error": "задача не найдена"}
            if job.get("status") in ("done", "failed", "expired"):
                if job.get("status") == "done":
                    return {"ok": True, "result": job.get("result")}
                return {"ok": False,
                        "error": job.get("error")
                        or f"задача {job.get('status')}"}
            await asyncio.sleep(poll_s)
        return {"ok": False, "error": f"таймаут {timeout_s}s ожидания"}

    def get_job(self, job_id: str) -> dict | None:
        with self._rl:
            jobs = self._load_jobs_maintained()
        for j in reversed(jobs):
            if j.get("id") == job_id:
                return j
        return None

    def list_jobs(self, limit: int = 30) -> list[dict]:
        with self._rl:
            jobs = self._load_jobs_maintained()
        return jobs[-limit:][::-1]

    def count_pending(self) -> int:
        with self._rl:
            jobs = self._load_jobs_maintained()
        return sum(1 for j in jobs if j.get("status") == "pending")

    # ═══════════════════════════════════════════════════════
    # Захваты (расширение → сервер)
    # ═══════════════════════════════════════════════════════
    def add_capture(self, cap: dict) -> dict:
        rec = {
            "id": _short_id(),
            "ts": _now(),
            "title": str(cap.get("title", ""))[:300],
            "url": str(cap.get("url", ""))[:2000],
            "text": str(cap.get("text", ""))[:CAPTURE_TEXT_MAX],
            "selection": str(cap.get("selection", ""))[:4000],
            "tab_id": cap.get("tab_id"),
            "source": str(cap.get("source", "extension")),
        }
        with self._rl, self._locked():
            items = self._read_jsonl(self.captures_path)
            items.append(rec)
            if len(items) > CAPTURES_MAX:       # вытесняем старые
                items = items[-CAPTURES_MAX:]
            self._write_jsonl_atomic(self.captures_path, items)
        return rec

    def list_captures(self, q: str = "", limit: int = 50,
                      offset: int = 0) -> dict:
        items = self._read_jsonl(self.captures_path)
        total_all = len(items)
        if q:
            needle = q.lower()
            items = [c for c in items
                     if needle in c.get("text", "").lower()
                     or needle in c.get("title", "").lower()
                     or needle in c.get("url", "").lower()
                     or needle in c.get("selection", "").lower()]
        matched = len(items)
        items = items[::-1][offset:offset + limit]   # свежие сверху
        return {"total_all": total_all, "matched": matched,
                "count": len(items), "captures": items}

    def get_capture(self, cap_id: str) -> dict | None:
        for c in self._read_jsonl(self.captures_path):
            if c.get("id") == cap_id:
                return c
        return None

    def delete_capture(self, cap_id: str) -> bool:
        with self._rl, self._locked():
            items = self._read_jsonl(self.captures_path)
            keep = [c for c in items if c.get("id") != cap_id]
            if len(keep) == len(items):
                return False
            self._write_jsonl_atomic(self.captures_path, keep)
        return True

    # ═══════════════════════════════════════════════════════
    # Диагностика
    # ═══════════════════════════════════════════════════════
    def status(self) -> dict:
        tabs = self.list_tabs()
        jobs = self._read_json(self.jobs_path, [])
        caps = self._read_jsonl(self.captures_path)
        statuses: dict[str, int] = {}
        for j in jobs:
            statuses[j.get("status", "?")] = \
                statuses.get(j.get("status", "?"), 0) + 1
        return {
            "dir": str(self.dir),
            "tabs": {"count": tabs["count"],
                     "age_s": tabs["age_s"]},
            "jobs": {"total": len(jobs),
                     "pending": statuses.get("pending", 0),
                     "dispatched": statuses.get("dispatched", 0),
                     "statuses": statuses},
            "captures": len(caps),
        }


def _clip_result(result: Any) -> Any:
    """Результат задачи может быть большим (текст страницы) — клипуем."""
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) > 120000:
            return {"_clipped": True,
                    "text": str(result)[:CAPTURE_TEXT_MAX]}
    except Exception:
        return str(result)[:CAPTURE_TEXT_MAX]
    return result


# ── Синглтон для импортёров ────────────────────────────────────
_default: BridgeStore | None = None


def get_store() -> BridgeStore:
    """Хранилище по умолчанию: <проект>/data/bridge (лениво)."""
    global _default
    if _default is None:
        root = Path(os.getenv("PROJECT_ROOT") or Path.cwd())
        _default = BridgeStore(root / "data" / "bridge")
    return _default
