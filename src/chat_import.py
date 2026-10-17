"""Импорт чатов с сайтов (DeepSeek/Qwen) — Task 27.

Пользователь выгружает чаты из веб-интерфейса провайдера (экспорт JSON)
и подгружает их в локальный чат через POST /api/chats/import. Каждый чат
экспорта становится локальной сессией (data/sessions/<id>.jsonl) — виден
в селекте «Последние чаты», участвует в контексте, правке и микрозадачах.

Формат DeepSeek (chat.deepseek.com → Настройки → Экспорт данных):
  [ { "title": "...", "created_at": 1789..., "updated_at": ...,
      "chat": { "messages": [ { "id", "role", "content", "content_list",
                                 "parentId", "childrenIds", "timestamp",
                                 "modelName", "reasoning_content", ... } ] } } ]

Сообщения — ДЕРЕВО (правки/регенерации создают ветки): идём от корня
(parentId=None) по childrenIds, в развилках берём последнюю ветку
(самую свежую регенерацию). Текст: content, а при пустом — content_list.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_IMPORT_MESSAGES = 500      # страховка на чат
# Дамп сессии должен читаться read_session_file (MAX_FILE_READ = 1 МБ) —
# иначе чат не появится в «Последних чатах». Ограничиваем сообщение и
# суммарный объём чата (бюджет с запасом до лимита читателя).
MAX_CONTENT_CHARS = 4_000
MAX_CHAT_BYTES = 800_000


def _msg_text(m: dict) -> str:
    """Текст сообщения: content → content_list (обрезка по бюджету)."""
    c = m.get("content")
    if isinstance(c, str) and c.strip():
        return c.strip()[:MAX_CONTENT_CHARS]
    cl = m.get("content_list")
    if isinstance(cl, list):
        parts: list[str] = []
        for seg in cl:
            if isinstance(seg, dict):
                t = seg.get("content") or seg.get("text") or ""
            elif isinstance(seg, str):
                t = seg
            else:
                t = ""
            if t:
                parts.append(str(t))
        joined = "\n".join(parts).strip()
        if joined:
            return joined[:MAX_CONTENT_CHARS]
    return ""


def _walk_tree(messages: list[dict]) -> list[dict]:
    """Активная ветка диалога из дерева parentId/childrenIds.

    Развилки (регенерации): берём последнего ребёнка по childrenIds,
    при наличии соответствия — уточняем по currentResponseIds/currentId.
    """
    by_id = {str(m.get("id")): m for m in messages if m.get("id")}
    roots = [m for m in messages if not m.get("parentId")
             or str(m.get("parentId")) not in by_id]
    if not roots:
        return []
    # упорядочим корни по timestamp (первый — старт диалога)
    roots.sort(key=lambda m: float(m.get("timestamp") or 0))

    path: list[dict] = []
    node = roots[0]
    seen: set[str] = set()
    while node is not None and str(node.get("id")) not in seen:
        seen.add(str(node.get("id")))
        path.append(node)
        kids = [by_id[str(k)] for k in (node.get("childrenIds") or [])
                if str(k) in by_id]
        if not kids:
            break
        kids.sort(key=lambda m: float(m.get("timestamp") or 0))
        node = kids[-1]              # самая свежая регенерация
    return path


def parse_deepseek_chat(item: dict) -> dict | None:
    """Один чат экспорта → {title, messages: [{role, content, ts}]}."""
    if not isinstance(item, dict):
        return None
    chat = item.get("chat") or {}
    raw = chat.get("messages")
    if not isinstance(raw, list) or not raw:
        return None

    # дерево или уже линейный список?
    has_links = any(m.get("parentId") or m.get("childrenIds") for m in raw)
    ordered = _walk_tree(raw) if has_links else list(raw)

    out: list[dict] = []
    used_bytes = 0
    for m in ordered:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        text = _msg_text(m)
        if not text:
            continue
        try:
            ts = float(m.get("timestamp") or 0)
        except Exception:
            ts = 0.0
        size = len(text.encode("utf-8", "replace"))
        if used_bytes + size > MAX_CHAT_BYTES:
            break        # бюджет дампа исчерпан — ранние сообщения важнее
        used_bytes += size
        out.append({"role": role, "content": text, "ts": ts})
        if len(out) >= MAX_IMPORT_MESSAGES:
            break
    if not out:
        return None
    title = str(item.get("title") or "").strip() or "Импорт из DeepSeek"
    return {"title": title[:80], "messages": out}


def parse_export(data: Any, provider: str = "deepseek") -> list[dict]:
    """Полный экспорт → список чатов. Ошибки отдельных чатов не роняют всё."""
    if isinstance(data, dict) and isinstance(data.get("chats"), list):
        data = data["chats"]          # вариант обёртки
    if not isinstance(data, list):
        raise ValueError("ожидается JSON-массив чатов (экспорт сайта)")
    chats: list[dict] = []
    for item in data:
        try:
            parsed = parse_deepseek_chat(item)
        except Exception:
            logger.debug("chat parse failed", exc_info=True)
            parsed = None
        if parsed:
            chats.append(parsed)
    return chats


def import_chats(data: Any, dump_dir: str | Path,
                 provider: str = "deepseek",
                 source_id: str = "") -> list[dict]:
    """Импортирует чаты в локальные сессии; возвращает список созданных.

    Формат дампа совместим с ConversationSession (jsonl: {"role", ...}) —
    селект чатов и микрозадачи подхватят их без дополнительных шагов.
    """
    chats = parse_export(data, provider)
    dump = Path(dump_dir)
    dump.mkdir(parents=True, exist_ok=True)
    created: list[dict] = []
    for chat in chats:
        sid = uuid.uuid4().hex[:16]
        path = dump / f"{sid}.jsonl"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"type": "title", "title": chat["title"],
                     "manual": False, "ts": time.time()},
                    ensure_ascii=False) + "\n")
                for m in chat["messages"]:
                    f.write(json.dumps(
                        {"role": m["role"], "content": m["content"],
                         "agent": "", "ts": m.get("ts") or time.time()},
                        ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("import dump failed: %s", path, exc_info=True)
            continue
        created.append({
            "session_id": sid, "title": chat["title"],
            "messages": len(chat["messages"]),
        })
    return created
