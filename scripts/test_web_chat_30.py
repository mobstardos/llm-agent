#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 30: транспорт веб-чатов по кукам (src/web_chat.py) на мок-сервере.

Проверяет БЕЗ реальных сайтов: SSE-парсинг обоих провайдеров, склейку
истории (_flatten_prompt), устойчивый парсер дельт, дружелюбные ошибки
401/403 и «нет кук», гейтинг web_cookie_providers/resolve_endpoint,
env-переопределение QWEN_WEB_MODEL.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import web_chat as wc                       # noqa: E402
from src import llm_providers as lp                  # noqa: E402
from src.llm_errors import describe                  # noqa: E402

PASS, FAIL = 0, 0
SERVER_STATE = {"requests": []}


def section(title: str) -> None:
    print(f"\n── {title} ─────────────────────────────────────────")


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    print(f"  [{mark}] {label}")
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1


# ── Мок-сервер: DeepSeek (create+completion) и Qwen (completion) ─────

class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # тишина в выводе
        pass

    def _sse(self, pieces, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for p in pieces:
            line = "data: " + json.dumps(p, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        SERVER_STATE["requests"].append(
            {"path": self.path,
             "headers": {k.lower(): v for k, v in self.headers.items()},
             "body": raw.decode("utf-8", "replace")})
        if self.path.endswith("/expired"):
            self._json({"error": "unauthorized"}, status=401)
            return
        if "/chat_session/create" in self.path:
            self._json({"code": 0, "data": {"biz_data": {
                "chat_session": {"id": "sess-1"}}}})
            return
        if "/chat/completion" in self.path:
            self._sse([
                {"v": {"response": "Привет"}},
                {"content": " "},
                {"choices": [{"delta": {"content": "мир"}}]},
                {"text": "!"},
            ])
            return
        self._json({"error": "not found"}, status=404)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main() -> int:
    srv, base = start_server()
    wc.DEEPSEEK_BASE = base
    wc.QWEN_BASE = base

    old_env = {k: os.environ.get(k, "") for k in
               ("DEEPSEEK_AUTH_TOKEN", "DEEPSEEK_DS_SESSION_ID",
                "DEEPSEEK_SMIDV2", "QWEN_WEB_TOKEN", "QWEN_WEB_COOKIES",
                "QWEN_WEB_MODEL")}
    os.environ["DEEPSEEK_AUTH_TOKEN"] = "dt-token"
    os.environ["DEEPSEEK_DS_SESSION_ID"] = "ds-sess"
    os.environ["DEEPSEEK_SMIDV2"] = "smid"
    os.environ["QWEN_WEB_TOKEN"] = "qw-token"
    os.environ["QWEN_WEB_COOKIES"] = "token=qw-token; c2=v2"

    try:
        # ── 1. Парсер дельт ──────────────────────────────────────
        section("1. _extract_piece: известные формы дельт")
        check(wc._extract_piece("сырой текст") == "сырой текст",
              "строка → как есть")
        check(wc._extract_piece({"content": "a"}) == "a", "content")
        check(wc._extract_piece({"text": "b"}) == "b", "text")
        check(wc._extract_piece({"v": {"response": "c"}}) == "c",
              "v.response (Qwen)")
        check(wc._extract_piece(
            {"choices": [{"delta": {"content": "d"}}]}) == "d",
              "choices.delta.content (OpenAI-подобные)")
        check(wc._extract_piece({"nope": 1}) == "", "неизвестное → пусто")

        # ── 2. Склейка истории ───────────────────────────────────
        section("2. _flatten_prompt: система + история + вопрос")
        prompt = wc._flatten_prompt([
            {"role": "system", "content": "Ты краток."},
            {"role": "user", "content": "вопрос 1"},
            {"role": "assistant", "content": "ответ 1"},
            {"role": "user", "content": "вопрос 2"},
        ])
        check("Ты краток." in prompt, "система в промпте")
        check("Пользователь: вопрос 1" in prompt, "история подписана")
        check(prompt.strip().endswith("вопрос 2"), "последний вопрос в конце")

        # ── 3. DeepSeek: полный цикл по кукам ────────────────────
        section("3. web_chat('deepseek'): create → SSE")
        tokens: list[str] = []

        async def _tok(piece: str) -> None:
            tokens.append(piece)

        stream_started: dict = {}
        res = asyncio.run(wc.web_chat(
            "deepseek",
            [{"role": "user", "content": "тест"}],
            on_token=_tok, stream_started=stream_started))
        check(res["content"] == "Привет мир!", f"склейка дельт: {res['content']!r}")
        check(res["web_provider"] == "deepseek", "метка web_provider")
        check(res["streamed"] is True and tokens, "on_token получал куски")
        check(stream_started.get("v") is True, "stream_started взведён")
        reqs = SERVER_STATE["requests"]
        create = next(r for r in reqs if "/chat_session/create" in r["path"])
        compl = next(r for r in reqs if r["path"].endswith("/chat/completion"))
        check(create["headers"].get("authorization") == "Bearer dt-token",
              "DeepSeek: Bearer из DEEPSEEK_AUTH_TOKEN")
        check("ds_session_id=ds-sess" in create["headers"].get("cookie", ""),
              "DeepSeek: кука ds_session_id в заголовке")
        body = json.loads(compl["body"])
        check(body["chat_session_id"] == "sess-1", "DeepSeek: session id из create")
        check("тест" in body["prompt"], "DeepSeek: промпт ушёл")

        # ── 4. Qwen: полный цикл + модель из env ─────────────────
        section("4. web_chat('qwen'): модель/токен/куки")
        SERVER_STATE["requests"].clear()
        res = asyncio.run(wc.web_chat(
            "qwen", [{"role": "user", "content": "привет"}]))
        check(res["content"] == "Привет мир!", "Qwen: склейка дельт")
        check(res["web_provider"] == "qwen", "метка web_provider=qwen")
        req = SERVER_STATE["requests"][0]
        qbody = json.loads(req["body"])
        check(qbody["model"] == "qwen3-max", "дефолт модели qwen3-max")
        check(req["headers"].get("authorization") == "Bearer qw-token",
              "Qwen: Bearer из QWEN_WEB_TOKEN")
        check("token=qw-token" in req["headers"].get("cookie", ""),
              "Qwen: Cookie из QWEN_WEB_COOKIES")
        os.environ["QWEN_WEB_MODEL"] = "qwen3-max-thinking"
        res = asyncio.run(wc.web_chat(
            "qwen", [{"role": "user", "content": "думай"}]))
        qbody = json.loads(SERVER_STATE["requests"][-1]["body"])
        check(qbody["model"] == "qwen3-max-thinking",
              "QWEN_WEB_MODEL переопределяет модель")
        os.environ.pop("QWEN_WEB_MODEL", None)

        # ── 5. Ошибки: 401 и нет кук ─────────────────────────────
        section("5. Ошибки: протухшие куки и их отсутствие")
        try:
            asyncio.run(wc.web_chat(
                "qwen", [{"role": "user", "content": "x"}],
                ) if False else _expired(wc))
            check(False, "401 должен кидать RuntimeError")
        except RuntimeError as e:
            check("куки/токен отклонены" in str(e),
                  "401 → понятный текст про куки")
            hint = describe(None, str(e))
            check("не авторизован" in hint and "Bridge" in hint,
                  "llm_errors.describe даёт подсказку про 🔑")
        for prov, key in (("deepseek", "DEEPSEEK_AUTH_TOKEN"),
                          ("qwen", "QWEN_WEB_TOKEN")):
            saved = {k: os.environ.pop(k) for k in
                     ("DEEPSEEK_AUTH_TOKEN", "DEEPSEEK_DS_SESSION_ID",
                      "DEEPSEEK_SMIDV2", "QWEN_WEB_TOKEN",
                      "QWEN_WEB_COOKIES")}
            try:
                asyncio.run(wc.web_chat(
                    prov, [{"role": "user", "content": "x"}]))
                check(False, f"{prov}: без кук должен быть RuntimeError")
            except RuntimeError as e:
                check(f"нет кук" in str(e).lower()
                      and prov in str(e).lower(),
                      f"{prov}: «нет кук …» в тексте")
                hint = describe(None, str(e))
                check("Bridge" in hint, f"{prov}: describe → подсказка Bridge")
            os.environ.update(saved)

        # ── 6. Реестр веб-провайдеров ────────────────────────────
        section("6. llm_providers: web_cookie_providers / resolve")
        provs = {p["id"] for p in lp.web_cookie_providers()}
        check(provs == {"deepseek_web", "qwen_web"},
              f"обе веб-модели в списке: {sorted(provs)}")
        ep = lp.resolve_endpoint("deepseek_web/web-chat")
        check(ep is not None and ep[0] == "web://deepseek",
              "resolve deepseek_web → web://deepseek")
        ep = lp.resolve_endpoint("qwen_web/web-chat")
        check(ep is not None and ep[0] == "web://qwen",
              "resolve qwen_web → web://qwen")
        check(lp.model_provider("qwen_web/web-chat") == "qwen_web",
              "model_provider знает qwen_web")
        check(not lp.endpoint_supports_tools("deepseek_web/web-chat"),
              "веб-чаты без tools (корректный фолбэк)")
    finally:
        for k, v in old_env.items():
            if v == "" and k in os.environ and v == "":
                os.environ.pop(k, None)
            elif v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        srv.shutdown()

    print("\n" + "═" * 60)
    print(f"ИТОГО: ✓ {PASS}  ✗ {FAIL}")
    return 0 if FAIL == 0 else 1


async def _expired(wc_mod) -> None:
    """Запрос к мок-эндпоинту /expired через _stream_sse (401 ветка)."""
    import httpx
    headers = wc_mod._base_headers("qwen")
    async with httpx.AsyncClient(timeout=10.0) as client:
        await wc_mod._stream_sse(
            client, "POST", f"{wc_mod.QWEN_BASE}/expired",
            headers, {}, None, None, label="qwen")


if __name__ == "__main__":
    sys.exit(main())
