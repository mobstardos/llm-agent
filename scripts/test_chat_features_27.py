#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 27 — самопроверка: правка/откат сообщений, веб-чаты по кукам,
импорт чатов DeepSeek, REST «Политик», UI-элементы. Запуск:
    python scripts/test_chat_features_27.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = 0
FAIL = 0


def check(cond: bool, label: str) -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [✓] {label}")
    else:
        FAIL += 1
        print(f"  [✗] {label}")


def section(title: str) -> None:
    print(f"\n═══ {title} ═══")


# ═══════════════════════════════════════════════════════════
# 1. Session: edit_user / откат / дамп
# ═══════════════════════════════════════════════════════════
section("session.py — правка сообщения и откат")

from src.supervisor.session import (  # noqa: E402
    ConversationSession, Msg,
)

with tempfile.TemporaryDirectory() as td:
    s = ConversationSession(dump_dir=td)
    s.add_user("вопрос 1")
    s.add_assistant("ответ 1", agent="file")
    s.add_user("вопрос 2")
    s.add_assistant("ответ 2")
    s.set_title("Тестовый чат")
    s.set_active_plan({"intent": "x", "steps": [{"id": 1, "status": "done"}]})
    s.record_step_result("1", {"agent": "file", "success": True,
                               "content": "ok"})

    view = s.messages_view()
    check([m["index"] for m in view] == [0, 1, 2, 3],
          "messages_view отдаёт индексы 0..3")
    check(view[0]["role"] == "user", "сообщение #0 — user")

    fixed = s.edit_user(0, "вопрос 1 (правка)")
    check(fixed == 0, "edit_user(0) вернул 0")
    check(len(s) == 1, "история откатилась до 1 сообщения")
    check(s.history[0].content == "вопрос 1 (правка)",
          "текст сообщения заменён")
    check(s.active_plan is None and not s.step_results,
          "план и шаги откачены")
    check(s.last_agents == [], "last_agents очищены")
    check(s.title == "Тестовый чат", "заголовок пережил правку")

    # дамп на диске перезаписан: 1 сообщение, без плана/шагов
    dump = Path(td) / f"{s.id}.jsonl"
    lines = [json.loads(x) for x in
             dump.read_text(encoding="utf-8").splitlines() if x.strip()]
    check(sum(1 for x in lines if "role" in x) == 1,
          "дамп перезаписан: 1 сообщение")
    check(not any(x.get("type") in ("plan", "step") for x in lines),
          "в дампе нет плана/шагов")
    check(any(x.get("type") == "title" for x in lines),
          "заголовок сохранён в дампе")

    # повторная загрузка с диска
    s2 = ConversationSession(session_id=s.id, dump_dir=td)
    s2.load()
    check(len(s2) == 1 and s2.history[0].content == "вопрос 1 (правка)",
          "перезагрузка с диска видит правку")

    # негативные случаи
    s3 = ConversationSession(dump_dir=td)
    s3.add_user("q")
    s3.add_assistant("a")
    check(s3.edit_user(1, "x") == -1, "правка assistant-сообщения — отказ")
    check(s3.edit_user(99, "x") == -1, "индекс за границей — отказ")
    check(s3.edit_user(0, "   ") == -1, "пустой текст — отказ")
    check(len(s3) == 2, "после отказов история не изменилась")

# ═══════════════════════════════════════════════════════════
# 2. chat_import.py — парсер экспорта DeepSeek
# ═══════════════════════════════════════════════════════════
section("chat_import.py — парсер экспорта")

from src.chat_import import (  # noqa: E402
    import_chats, parse_deepseek_chat, parse_export,
)


def msg(mid, role, parent=None, kids=None, content="", ts=1.0, clist=None):
    d = {"id": mid, "role": role, "content": content, "timestamp": ts}
    if parent:
        d["parentId"] = parent
    if kids:
        d["childrenIds"] = kids
    if clist:
        d["content_list"] = clist
    return d


# дерево: user → a1 → user2 → (a2 старая, a3 свежая регенерация)
chat = {
    "title": "Дерево",
    "chat": {"messages": [
        msg("u1", "user", content="привет", ts=1, kids=["a1"]),
        msg("a1", "assistant", parent="u1", kids=["u2"], ts=2,
            content="ответ 1"),
        msg("u2", "user", parent="a1", kids=["a2", "a3"], ts=3,
            content="вопрос 2"),
        msg("a2", "assistant", parent="u2", ts=4, content="старая ветка"),
        msg("a3", "assistant", parent="u2", ts=5, content="свежая ветка"),
    ]},
}
parsed = parse_deepseek_chat(chat)
check(parsed is not None, "чат с деревом распарсен")
check([m["role"] for m in parsed["messages"]] ==
      ["user", "assistant", "user", "assistant"],
      "активная ветка: 4 сообщения")
check(parsed["messages"][-1]["content"] == "свежая ветка",
      "в развилке взята свежая регенерация")
check(parsed["title"] == "Дерево", "заголовок сохранён")

# content_list fallback + пустой content
chat2 = {"title": "", "chat": {"messages": [
    msg("u1", "user", content="", kids=["a1"]),
    msg("a1", "assistant", parent="u1", content="",
        clist=[{"content": "часть 1"}, {"content": "часть 2"}]),
]}}
parsed2 = parse_deepseek_chat(chat2)
check(parsed2["title"].startswith("Импорт"), "пустой title → дефолт")
check(parsed2["messages"][0]["content"] == "часть 1\nчасть 2",
      "content_list склеен")

# чужие роли и мусор
chat3 = {"title": "x", "chat": {"messages": [
    {"role": "system", "content": "не берём"},
    {"role": "user"},                       # пусто
    msg("u1", "user", content="ок"),
]}}
parsed3 = parse_deepseek_chat(chat3)
check(len(parsed3["messages"]) == 1, "системные/пустые сообщения отфильтрованы")

# бюджет байтов: длинные сообщения обрезаются по MAX_CHAT_BYTES
from src.chat_import import MAX_CHAT_BYTES  # noqa: E402
big = {"title": "big", "chat": {"messages": [
    msg(f"m{i}", "user", content="ж" * 50_000, ts=i) for i in range(30)
]}}
parsed4 = parse_deepseek_chat(big)
total = sum(len(m["content"].encode()) for m in parsed4["messages"])
check(total <= MAX_CHAT_BYTES, f"бюджет {MAX_CHAT_BYTES} байт соблюдён "
                               f"(факт {total})")
check(len(parsed4["messages"][0]["content"]) <= 4000,
      "одно сообщение ≤ 4000 символов")

# parse_export: массив чатов + ошибка формы
two = parse_export([chat, chat2])
check(len(two) == 2, "массив из 2 чатов → 2 записи")
try:
    parse_export({"not": "a list"})
    check(False, "не-массив должен кидать ValueError")
except ValueError:
    check(True, "не-массив кидает ValueError")

# import_chats пишет дампы, читаемые ConversationSession
with tempfile.TemporaryDirectory() as td:
    created = import_chats([chat], td)
    check(len(created) == 1 and created[0]["messages"] == 4,
          "import_chats: 1 чат, 4 сообщения")
    sess = ConversationSession(session_id=created[0]["session_id"],
                               dump_dir=td)
    sess.load()
    check(len(sess) == 4, "ConversationSession.load() читает дамп импорта")
    check(sess.title == "Дерево", "заголовок подхвачен сессией")

# ═══════════════════════════════════════════════════════════
# 3. Веб-чаты по кукам: providers/endpoint/aggregate
# ═══════════════════════════════════════════════════════════
section("llm_providers.py — веб-чаты по кукам")

from src import llm_providers as lp  # noqa: E402

os.environ.pop("DEEPSEEK_SMIDV2", None)
os.environ.pop("DEEPSEEK_AUTH_TOKEN", None)
os.environ.pop("DEEPSEEK_DS_SESSION_ID", None)
os.environ.pop("QWEN_WEB_COOKIES", None)
os.environ.pop("QWEN_WEB_TOKEN", None)
lp._clear_settings_cache if hasattr(lp, "_clear_settings_cache") else None

check(lp.web_cookie_providers() == [], "без кук — ни одного веб-провайдера")
# resolve_endpoint резолвит всегда: без кук запрос упадёт в web_chat с
# внятной ошибкой «нет кук» + сработает фолбэк, а не молчаливый дефолт;
# из списка моделей провайдера убирает aggregate()
ep0 = lp.resolve_endpoint("deepseek_web/web-chat")
check(ep0 and ep0[0] == "web://deepseek",
      "без кук resolve_endpoint всё равно ведёт на web:// (явная ошибка)")

os.environ["DEEPSEEK_SMIDV2"] = "test"
provs = lp.web_cookie_providers()
check([p["id"] for p in provs] == ["deepseek_web"], "кука → deepseek_web")
check(provs[0]["available"] and provs[0]["models"][0]["id"] == "web-chat",
      "deepseek_web доступен с моделью web-chat")
ep = lp.resolve_endpoint("deepseek_web/web-chat")
check(ep and ep[0] == "web://deepseek" and
      ep[2].get("web_provider") == "deepseek",
      "resolve_endpoint → web://deepseek + meta")
check(lp.model_provider("deepseek_web/web-chat") == "deepseek_web",
      "model_provider знает deepseek_web")
check(not lp.endpoint_supports_tools("deepseek_web/web-chat"),
      "веб-чат без tools")
check(lp.is_local_model("deepseek_web/web-chat") is False,
      "веб-чат не локальная модель")

os.environ["QWEN_WEB_COOKIES"] = "token=abc; other=x"
provs = lp.web_cookie_providers()
check([p["id"] for p in provs] == ["deepseek_web", "qwen_web"],
      "+ qwen_web по куки")
agg = asyncio.run(lp.aggregate())
web_ids = [p["id"] for p in agg["providers"] if p["id"].endswith("_web")]
check(web_ids == ["deepseek_web", "qwen_web"],
      "aggregate включает оба веб-провайдера")

from src.web_chat import _cookie_token_from_header  # noqa: E402
check(_cookie_token_from_header() == "abc", "token извлечён из куки-строки")

# ═══════════════════════════════════════════════════════════
# 4. llm_client — маршрутизация в web_chat
# ═══════════════════════════════════════════════════════════
section("llm_client.py — маршрутизация веб-чатов")

from src.llm_client import LLMClient  # noqa: E402

client = LLMClient()
cli, mdl, meta = client._client_for("deepseek_web/web-chat")
check(cli is None and meta.get("web_provider") == "deepseek",
      "_client_for: web-модель уходит из OpenAI-пути")

called = {}


async def fake_web_chat(provider, messages, on_token=None,
                        stream_started=None, **kw):
    called["provider"] = provider
    called["messages"] = messages
    called["stream_started"] = stream_started
    if on_token:
        await on_token("ок")
    return {"role": "assistant", "content": "ок", "chunks": [],
            "streamed": bool(on_token), "web_provider": provider}


import src.web_chat as _wc  # noqa: E402
_orig = _wc.web_chat
_wc.web_chat = fake_web_chat
try:
    res = asyncio.run(client.chat(
        [{"role": "user", "content": "пинг"}],
        tools=[{"fake": "tool"}],
        model="deepseek_web/web-chat",
        on_token=lambda t: asyncio.sleep(0),
    ))
finally:
    _wc.web_chat = _orig
check(called.get("provider") == "deepseek",
      "chat() роутит в web_chat(deepseek)")
check(res.get("content") == "ок", "ответ веб-чата возвращён")
check(res.get("web_provider") == "deepseek",
      "результат помечен web_provider")

# ═══════════════════════════════════════════════════════════
# 5. main.py — маркеры новых эндпоинтов и WS-веток
# ═══════════════════════════════════════════════════════════
section("main.py — эндпоинты и WS")

src_main = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
for marker in (
    'mtype == "edit_message"', '"type": "session_edited"',
    '"_regen": True', "regen = bool(payload.get(\"_regen\"))",
    "if not regen:", '"/api/chats/import"', '"/api/policies"',
    '"/api/policies/metrics"', '"/api/policies/clear"',
    '"/api/policies/cleanup"', '"/api/policies/backup"',
    '"/api/policies/export"', '"/api/policies/import/preview"',
    '"/api/policies/import"', "request.form()",
):
    check(marker in src_main, f"main.py: {marker}")

# ═══════════════════════════════════════════════════════════
# 6. UI: app.js / index.html / style.css
# ═══════════════════════════════════════════════════════════
section("UI — правка, импорт, кнопки")

app_js = (ROOT / "src" / "web" / "app.js").read_text(encoding="utf-8")
r = subprocess.run(["node", "--check", str(ROOT / "src/web/app.js")],
                   capture_output=True)
check(r.returncode == 0, "app.js: node --check")
for marker in (
    'type: "edit_message"', "case \"session_edited\"",
    "function startEdit", "function cancelEdit", "function sendEdit",
    "function rerenderFromView", "chatIndex++", "addMsg(\"user\", text,"
    " \"\", chatIndex++)", "$(\"import-chats\")", "$(\"import-file\")",
    "/api/chats/import?provider=deepseek", "edit-cancel",
    "$(\"import-chats\")", "$(\"import-file\")", "$(\"edit-banner\")",
):
    check(marker in app_js, f"app.js: {marker}")

idx_html = (ROOT / "src" / "web" / "index.html").read_text(encoding="utf-8")
for marker in ("id=\"edit-banner\"", "id=\"edit-cancel\"",
               "id=\"import-chats\"", "id=\"import-file\"",
               "Импорт чатов с сайта"):
    check(marker in idx_html, f"index.html: {marker}")

style = (ROOT / "src" / "web" / "style.css").read_text(encoding="utf-8")
for marker in (".msg-actions", ".msg-edit-btn", ".edit-banner",
               ".msg.user:hover .msg-actions"):
    check(marker in style, f"style.css: {marker}")

req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
check("python-multipart" in req, "requirements.txt: python-multipart")

# ═══════════════════════════════════════════════════════════
print(f"\n═══ Итог: {OK}/{OK + FAIL} проверок зелёные ═══")
sys.exit(0 if FAIL == 0 else 1)
