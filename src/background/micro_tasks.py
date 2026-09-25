"""Локальные фоновые микрозадачи на малой модели Ollama (Task 24-c).

Роль — «уборочная машина» для простых задач, чтобы не тратить облако:
  1. Заголовки чатов  — 2–4 слова по первым сообщениям сессии
     (data/sessions/<id>.jsonl, аддитивная запись {type: "title"}).
     Ручные заголовки (manual=true) никогда не перезаписываются.
  2. Ключевые слова   — 3–7 тегов на каждое новое сообщение
     пользователя (запись {type: "tags"}); задел под пре-фильтр
     pgvector-памяти.
  3. Заголовки планов — короткое название для планов Supervisor
     без title (PlanRegistry.update(title=...)).
  4. Дайджест журнала — суммаризация событий JournalStore за период
     → data/micro/digest.json + GET /api/digest/latest.

Политика VRAM (2 ГБ): одна модель на всё; пока в чате выбрана
локальная модель (state["local_chat_active_until"]), цикл воркера
пропускается — без свопов моделей. Все вызовы Ollama с малыми
num_predict и температурой 0.2; любые ошибки — мягко, воркер не падает.

Env:
  LOCAL_MICRO_TASKS_ENABLED=1   включение воркера (дефолт — вкл)
  LOCAL_MICRO_MODEL=qwen2.5:1.5b-instruct   модель микрозадач
  LOCAL_MICRO_INTERVAL=60       период цикла, сек
  LOCAL_MICRO_DIGEST_INTERVAL=3600   период дайджеста, сек
  LOCAL_MICRO_MAX_TITLE_BUDGET=8     заголовков за цикл
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from src.ollama.client import OllamaClient

logger = logging.getLogger(__name__)

MAX_FILE_READ = 1024 * 1024          # не читать сессии-гиганты целиком
MAX_MSG_PREVIEW = 300                # сколько символов сообщения в промпт
MAX_DIGEST_LINES = 120               # строк журнала в промпт дайджеста
MAX_DIGEST_LINE = 140                # символов на строку журнала


# ═══════════════════════════════════════════════════════════════════════
# Разбор дампа сессии (jsonl: {role...} | {type: plan|step|title|tags})
# ═══════════════════════════════════════════════════════════════════════

def read_session_file(path: Path) -> dict:
    """Мягкий разбор дампа сессии: сообщения + заголовок + теги по счёту."""
    out: dict[str, Any] = {
        "path": path,
        "id": path.stem,
        "msgs": [],
        "title": None,          # {title, manual} | None
        "title_manual": False,
        "tags_records": 0,      # сколько записей tags уже в файле
        "mtime": 0.0,
    }
    try:
        out["mtime"] = path.stat().st_mtime
        if path.stat().st_size > MAX_FILE_READ:
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if "role" in data:
                out["msgs"].append({
                    "role": str(data.get("role", "user")),
                    "content": str(data.get("content", "")),
                    "ts": float(data.get("ts", 0) or 0),
                })
            elif data.get("type") == "title":
                out["title"] = str(data.get("title", ""))
                out["title_manual"] = bool(data.get("manual"))
            elif data.get("type") == "tags":
                out["tags_records"] += 1
    except Exception:
        logger.debug("read_session_file failed: %s", path, exc_info=True)
    return out


def scan_sessions(sessions_dir: Path, limit: int = 20) -> dict:
    """Список последних чатов для GET /api/sessions."""
    sessions: list[dict] = []
    try:
        if sessions_dir.is_dir():
            files = sorted(
                sessions_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )[: max(1, limit) * 3]
        else:
            files = []
        for p in files:
            info = read_session_file(p)
            user_msgs = [m for m in info["msgs"] if m["role"] == "user"]
            if not info["msgs"] and not info["title"]:
                continue
            preview = (user_msgs[0]["content"] if user_msgs else "")[:120]
            sessions.append({
                "id": info["id"],
                "title": info["title"] or "",
                "title_manual": info["title_manual"],
                "messages": len(info["msgs"]),
                "updated_at": max(
                    [m["ts"] for m in info["msgs"]] + [info["mtime"]]
                ),
                "preview": preview,
            })
            if len(sessions) >= limit:
                break
    except Exception:
        logger.debug("scan_sessions failed", exc_info=True)
    return {"sessions": sessions}


def fallback_title(text: str) -> str:
    """Детерминированный заголовок без модели: первые 5 слов."""
    words = [w for w in "".join(
        c if c.isalnum() or c.isspace() else " " for c in (text or "")
    ).split() if w]
    if not words:
        return ""
    title = " ".join(words[:5]).strip()
    return title[:60]


# ═══════════════════════════════════════════════════════════════════════
# Воркер
# ═══════════════════════════════════════════════════════════════════════

class MicroTasksWorker:
    """Фоновый воркер микрозадач (одна модель Ollama на всё)."""

    def __init__(
        self,
        ollama: OllamaClient,
        sessions_dir: Path,
        *,
        plans_registry: Any = None,
        journal_getter: Callable[[], Any] | None = None,
        pause_check: Callable[[], bool] | None = None,
        base_dir: Path | None = None,
    ):
        self.ollama = ollama
        self.model = os.getenv(
            "LOCAL_MICRO_MODEL", "",
        ).strip() or ollama.model
        self.sessions_dir = Path(sessions_dir)
        self.plans = plans_registry
        self.journal_getter = journal_getter
        self.pause_check = pause_check
        base = Path(base_dir) if base_dir else Path(".")
        self.micro_dir = base / "data" / "micro"
        self.state_path = self.micro_dir / "state.json"
        self.digest_path = self.micro_dir / "digest.json"

        self.interval = float(os.getenv("LOCAL_MICRO_INTERVAL", "60"))
        self.digest_interval = float(
            os.getenv("LOCAL_MICRO_DIGEST_INTERVAL", "3600")
        )
        self.title_budget = int(
            os.getenv("LOCAL_MICRO_MAX_TITLE_BUDGET", "8")
        )

        self._running = False
        self._task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._digest_lock = asyncio.Lock()
        self._ollama_ok: bool | None = None      # None — ещё не проверяли

        # счётчики/метрики
        self.titles_generated = 0
        self.tags_generated = 0
        self.digests_generated = 0
        self.last_digest: dict | None = None
        self.last_cycle_at = 0.0

        self._state = self._load_state()

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "MicroTasksWorker started (model=%s, interval=%.0fs, "
            "digest_interval=%.0fs)",
            self.model, self.interval, self.digest_interval,
        )

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._save_state()
        logger.info("MicroTasksWorker stopped")

    def wakeup(self) -> None:
        """Разбудить внеочередным циклом (кнопка в UI и т.п.)."""
        self._wakeup.set()

    def stats(self) -> dict:
        return {
            "running": self._running,
            "model": self.model,
            "interval": self.interval,
            "titles_generated": self.titles_generated,
            "tags_generated": self.tags_generated,
            "digests_generated": self.digests_generated,
            "last_cycle_at": self.last_cycle_at,
        }

    # ═══════════════════════════════════════════════════════
    # Основной цикл
    # ═══════════════════════════════════════════════════════
    async def _run_loop(self) -> None:
        while self._running:
            try:
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(), timeout=self.interval,
                    )
                    self._wakeup.clear()
                except asyncio.TimeoutError:
                    pass

                if not self._running:
                    return
                # VRAM-пауза: в чате активна локальная модель
                if self.pause_check and self.pause_check():
                    logger.debug("Micro: пауза — активен локальный чат")
                    continue
                # Ollama недоступна — тихо ждём (лог раз в ~10 минут)
                ok = await self.ollama.health()
                if ok != self._ollama_ok:
                    logger.info(
                        "Micro: Ollama %s",
                        "доступна" if ok else "недоступна",
                    )
                    self._ollama_ok = ok
                if not ok:
                    continue

                self.last_cycle_at = time.time()
                await self._run_cycle()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Micro cycle error: %s", e)
                await asyncio.sleep(10)

    async def _run_cycle(self) -> None:
        try:
            await self._title_sessions()
        except Exception as e:
            logger.debug("Micro titles: %s", e)
        try:
            await self._tag_sessions()
        except Exception as e:
            logger.debug("Micro tags: %s", e)
        try:
            await self._title_plans()
        except Exception as e:
            logger.debug("Micro plan titles: %s", e)
        try:
            await self._digest_if_due()
        except Exception as e:
            logger.debug("Micro digest: %s", e)
        self._save_state()

    # ═══════════════════════════════════════════════════════
    # 1. Заголовки чатов
    # ═══════════════════════════════════════════════════════
    def _recent_session_files(self, limit: int = 50) -> list[Path]:
        try:
            if not self.sessions_dir.is_dir():
                return []
            return sorted(
                self.sessions_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )[:limit]
        except Exception:
            return []

    async def _title_sessions(self) -> None:
        made = 0
        for path in self._recent_session_files():
            if made >= self.title_budget:
                break
            info = read_session_file(path)
            if info["title"] is not None:
                continue                     # уже есть (авто или ручной)
            user_msgs = [m for m in info["msgs"] if m["role"] == "user"]
            if not user_msgs:
                continue
            assistant_msgs = [
                m for m in info["msgs"] if m["role"] == "assistant"
            ]
            if not assistant_msgs:
                continue                     # диалог ещё не начался
            first_user = user_msgs[0]["content"][:MAX_MSG_PREVIEW].strip()
            first_reply = (assistant_msgs[0]["content"]
                           or "")[:200].strip()
            if len(first_user) < 8:
                title = fallback_title(first_user)
            else:
                raw = await self.ollama.chat(
                    [
                        {"role": "system",
                         "content": "Ты придумываешь короткие заголовки "
                                    "чатов на русском. Отвечай только JSON."},
                        {"role": "user",
                         "content":
                             "Придумай заголовок из 2–4 слов по началу "
                             "диалога. Без кавычек и точек.\n\n"
                             f"Пользователь: {first_user}\n"
                             f"Ассистент: {first_reply}"},
                    ],
                    format_json=True, temperature=0.2,
                    model=self.model, max_tokens=32,
                )
                title = self._extract_field(raw, "title")
            if not title:
                title = fallback_title(first_user)
            if title:
                self._append_record(path, {
                    "type": "title", "title": title[:80],
                    "manual": False, "ts": time.time(),
                })
                made += 1
                self.titles_generated += 1
                logger.info("Micro: заголовок чата %s → %r",
                            path.stem, title[:40])

    # ═══════════════════════════════════════════════════════
    # 2. Теги сообщений пользователя
    # ═══════════════════════════════════════════════════════
    async def _tag_sessions(self, budget: int = 15) -> None:
        tagged_total = 0
        for path in self._recent_session_files():
            if tagged_total >= budget:
                break
            info = read_session_file(path)
            user_msgs = [m for m in info["msgs"] if m["role"] == "user"]
            done = int(
                self._state.get("sessions", {})
                .get(info["id"], {}).get("tagged", 0)
            )
            # записи tags добавляются в конец файла: смещение = msgs − done
            todo = user_msgs[done: done + max(0, budget - tagged_total)]
            if not todo:
                # догонали файл — чистим счётчик, чтобы state не рос
                if done > len(user_msgs) and info["id"] in (
                    self._state.get("sessions") or {}
                ):
                    self._state["sessions"].pop(info["id"], None)
                continue
            for msg in todo:
                text = (msg["content"] or "").strip()
                if len(text) < 12:
                    continue
                raw = await self.ollama.chat(
                    [
                        {"role": "system",
                         "content": "Ты извлекаешь ключевые слова для "
                                    "поиска. Отвечай только JSON."},
                        {"role": "user",
                         "content":
                             "Извлеки 3–7 ключевых слов/терминов из "
                             "сообщения. Низко регистра, без стоп-слов.\n\n"
                             f"{text[:MAX_MSG_PREVIEW]}"},
                    ],
                    format_json=True, temperature=0.1,
                    model=self.model, max_tokens=48,
                )
                tags = self._extract_list(raw, "tags")
                if tags:
                    self._append_record(path, {
                        "type": "tags", "tags": tags[:7],
                        "at": msg["ts"] or time.time(),
                    })
                    tagged_total += 1
                    self.tags_generated += 1
            # фиксируем прогресс по файлу (сколько user-сообщений отработано)
            sessions_state = self._state.setdefault("sessions", {})
            sessions_state[info["id"]] = {
                "tagged": min(len(user_msgs), done + len(todo)),
            }

    # ═══════════════════════════════════════════════════════
    # 3. Заголовки планов Supervisor
    # ═══════════════════════════════════════════════════════
    async def _title_plans(self, budget: int = 5) -> None:
        if self.plans is None:
            return
        try:
            records = self.plans.list(limit=40)
        except Exception:
            return
        made = 0
        for rec in records:
            if made >= budget:
                break
            pid = str(rec.get("plan_id") or "")
            if not pid or rec.get("title"):
                continue
            attempts = int(
                self._state.get("plan_attempts", {}).get(pid, 0)
            )
            if attempts >= 2:
                continue
            query = str(rec.get("query") or "").strip()
            if len(query) < 8:
                # нет материала — ставим детерминированный и не трогаем
                self.plans.update(pid, title=fallback_title(query) or pid[:8])
                continue
            raw = await self.ollama.chat(
                [
                    {"role": "system",
                     "content": "Ты придумываешь короткие названия задач. "
                                "Отвечай только JSON."},
                    {"role": "user",
                     "content":
                         "Название задачи из 2–5 слов по запросу "
                         "пользователя:\n\n" + query[:MAX_MSG_PREVIEW]},
                ],
                format_json=True, temperature=0.2,
                model=self.model, max_tokens=32,
            )
            title = self._extract_field(raw, "title") or fallback_title(query)
            self.plans.update(pid, title=(title or "")[:80])
            self._state.setdefault("plan_attempts", {})[pid] = attempts + 1
            made += 1

    # ═══════════════════════════════════════════════════════
    # 4. Дайджест журнала
    # ═══════════════════════════════════════════════════════
    async def force_digest(self) -> dict:
        """Внеплановый дайджест (кнопка в UI). Возвращает результат."""
        async with self._digest_lock:
            await self._build_digest()
            return self.last_digest or {}

    async def _digest_if_due(self) -> None:
        last_ts = float(self._state.get("last_digest_ts", 0) or 0)
        if time.time() - last_ts < self.digest_interval:
            return
        async with self._digest_lock:
            # повторная проверка под замком (force_digest мог обогнать)
            last_ts = float(self._state.get("last_digest_ts", 0) or 0)
            if time.time() - last_ts < self.digest_interval:
                return
            await self._build_digest()

    async def _build_digest(self) -> None:
        lines, source, count, until, since = self._collect_events()
        if not lines:
            digest_text = "Событий не было."
        else:
            blob = "\n".join(lines)[: MAX_DIGEST_LINES * MAX_DIGEST_LINE]
            digest_text = (await self.ollama.chat(
                [
                    {"role": "system",
                     "content": "Ты кратко резюмируешь журнал событий "
                                "агентной системы. Отвечай по-русски."},
                    {"role": "user",
                     "content":
                         "Сделай дайджест: 3–7 строк с префиксом «•» — что "
                         "происходило в системе. Без технического мусора.\n\n"
                         "События:\n" + blob},
                ],
                temperature=0.2,
                model=self.model, max_tokens=300,
            ) or "").strip() or "Событий не было."
        self.last_digest = {
            "ts": time.time(),
            "since": since,
            "until": until,
            "source": source,
            "events": count,
            "text": digest_text[:2000],
        }
        self.digests_generated += 1
        self._state["last_digest_ts"] = until or time.time()
        self._save_digest()
        logger.info(
            "Micro: дайджест готов (%s, %d событий)", source, count,
        )

    def _collect_events(
        self,
    ) -> tuple[list[str], str, int, float, float]:
        """Строки журнала за период: JournalStore → фолбэк — шина событий."""
        now = time.time()
        since = float(self._state.get("last_digest_ts", 0) or 0)
        journal = self.journal_getter() if self.journal_getter else None
        rec_store = getattr(journal, "store", None) if journal else None
        if rec_store is not None:
            try:
                events = rec_store.query(
                    since=since or None, until=now, limit=1000,
                    order_desc=False,
                )
                lines = []
                for ev in events:
                    action = (getattr(ev, "action", "")
                              or getattr(ev, "tool_name", "") or "event")
                    srv = getattr(ev, "server_name", "") or ""
                    agent = getattr(ev, "agent_id", "") or ""
                    status = getattr(ev, "status", "")
                    err = getattr(ev, "error", "") or ""
                    line = f"[{getattr(ev, 'kind', '')}] {action}"
                    if srv:
                        line += f" ({srv})"
                    line += f" → {status}"
                    if agent:
                        line += f" [{agent}]"
                    if err:
                        line += f" !! {err[:80]}"
                    lines.append(line[:MAX_DIGEST_LINE])
                return lines, "журнал", len(events), now, since
            except Exception as e:
                logger.debug("Journal query failed: %s", e)
        # фолбэк: недавние события шины (в памяти, только свежие)
        try:
            from src import events as bus
            rows = [r for r in bus.recent(200)
                    if float(r.get("ts", 0) or 0) > since]
            lines = [
                f"[{r.get('kind', '')}] {r.get('kind', 'event')} "
                f"{json.dumps(r.get('detail', ''), ensure_ascii=False)[:80]}"
                for r in rows
            ]
            return lines, "шина событий", len(rows), now, since
        except Exception:
            return [], "нет источника", 0, now, since

    # ═══════════════════════════════════════════════════════
    # Утилиты
    # ═══════════════════════════════════════════════════════
    @staticmethod
    def _extract_field(raw: str, field: str) -> str:
        """Достать строку из JSON-ответа (с очисткой мусора).

        Невалидный JSON → пустая строка (сработает детерминированный
        fallback_title, а не сырой текст модели).
        """
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except Exception:
            return ""
        val = str(data.get(field, "") or "").strip() if isinstance(
            data, dict,
        ) else ""
        val = val.replace("\n", " ").strip().strip('"').strip()
        return val[:80]

    @staticmethod
    def _extract_list(raw: str, field: str) -> list[str]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
            val = data.get(field) or []
            if isinstance(val, list):
                return [str(v).strip() for v in val if str(v).strip()][:7]
            if isinstance(val, str):
                return [
                    p.strip() for p in val.split(",") if p.strip()
                ][:7]
        except Exception:
            pass
        return []

    @staticmethod
    def _append_record(path: Path, record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("append_record failed: %s", path, exc_info=True)

    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                data = json.loads(
                    self.state_path.read_text(encoding="utf-8")
                )
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self.micro_dir.mkdir(parents=True, exist_ok=True)
            # ограничиваем рост state: максимум 500 сессий / 500 планов
            sessions = self._state.get("sessions") or {}
            if len(sessions) > 500:
                self._state["sessions"] = dict(
                    list(sessions.items())[-500:]
                )
            attempts = self._state.get("plan_attempts") or {}
            if len(attempts) > 500:
                self._state["plan_attempts"] = dict(
                    list(attempts.items())[-500:]
                )
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.state_path)
        except Exception:
            logger.debug("save_state failed", exc_info=True)

    def _save_digest(self) -> None:
        try:
            self.micro_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.digest_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.last_digest or {}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.digest_path)
        except Exception:
            logger.debug("save_digest failed", exc_info=True)

    def load_digest_from_disk(self) -> None:
        """Подхватить последний дайджест после рестарта сервера."""
        try:
            if self.digest_path.exists() and self.last_digest is None:
                data = json.loads(
                    self.digest_path.read_text(encoding="utf-8")
                )
                if isinstance(data, dict) and data.get("text"):
                    self.last_digest = data
        except Exception:
            pass
