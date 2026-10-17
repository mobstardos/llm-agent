"""Веб-чаты DeepSeek/Qwen по кукам из расширения Bridge — Task 27.

Куки собирает расширение «LLM Agent Bridge» (Task 24-a) → POST
/api/bridge/cookies → env (DEEPSEEK_*, QWEN_WEB_*). Когда они есть,
в списке моделей появляются провайдеры deepseek_web / qwen_web
(llm_providers.web_cookie_providers), а LLMClient маршрутизирует
запросы сюда (meta["web_provider"]).

DeepSeek: официальный приватный API веб-чата — тот же, что у MCP-сервера
src/mcp_servers/deepseek/server.py (chat_session/create → chat/completion,
SSE). Разговор сворачивается в один промпт (у веб-чата нет messages-массива).

Qwen: chat.qwen.ai — Bearer-токен из куки token; эндпоинт /api/chat/completion
(SSE). Формат парсится устойчиво (несколько известных форм дельты) — при
ошибке наверх уходит исключение и срабатывает штатный фолбэк LLMClient.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], Awaitable[None]]

DEEPSEEK_BASE = "https://chat.deepseek.com"
QWEN_BASE = "https://chat.qwen.ai"
THUMBCACHE_NAME = ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae"
_POW_SOLVER_CJS = os.path.join(os.path.dirname(__file__), "pow_solver.cjs")
_POW_WASM = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "pow", "sha3_wasm_bg.wasm")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ── Публичный статус (для списка моделей) ─────────────────────────────

DEEPSEEK_ENV = ("DEEPSEEK_AUTH_TOKEN", "DEEPSEEK_DS_SESSION_ID",
                "DEEPSEEK_SMIDV2")
QWEN_ENV = ("QWEN_WEB_TOKEN", "QWEN_WEB_COOKIES")


def web_cookie_available(provider: str) -> bool:
    """Есть ли куки провайдера в окружении (без чтения значений)."""
    envs = DEEPSEEK_ENV if provider == "deepseek" else QWEN_ENV
    return any(os.getenv(e, "").strip() for e in envs)


def _cookie_header(provider: str) -> str:
    """Cookie-строка из env (qwen — целиком из QWEN_WEB_COOKIES)."""
    if provider == "deepseek":
        parts: list[str] = []
        if v := os.getenv("DEEPSEEK_DS_SESSION_ID", "").strip():
            parts.append(f"ds_session_id={v}")
        if v := os.getenv("DEEPSEEK_SMIDV2", "").strip():
            parts.append(f"smidV2={v}")
        if v := os.getenv("DEEPSEEK_THUMBCACHE", "").strip():
            parts.append(f"{THUMBCACHE_NAME}={v}")
        return "; ".join(parts)
    return os.getenv("QWEN_WEB_COOKIES", "").strip()


def _bearer(provider: str) -> str:
    if provider == "deepseek":
        raw = (os.getenv("DEEPSEEK_AUTH_TOKEN", "").strip()
               or os.getenv("DEEPSEEK_SMIDV2", "").strip())
        # localStorage-токен мог сохраниться JSON-обёрткой {"value": ...}
        if raw.startswith("{"):
            from src.web_cookies import _clean_user_token
            raw = _clean_user_token(raw)
        return raw
    return (os.getenv("QWEN_WEB_TOKEN", "").strip()
            or _cookie_token_from_header())


def _cookie_token_from_header() -> str:
    """token=... внутри QWEN_WEB_COOKIES."""
    for pair in os.getenv("QWEN_WEB_COOKIES", "").split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == "token":
                return v.strip()
    return ""


def _base_headers(provider: str) -> dict[str, str]:
    base = DEEPSEEK_BASE if provider == "deepseek" else QWEN_BASE
    h = {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": base,
        "referer": f"{base}/",
        "user-agent": _UA,
    }
    if provider == "deepseek":
        h["x-app-version"] = "20241129.1"
        h["x-client-platform"] = "web"
        h["x-client-version"] = "1.0.0-always"
    ck = _cookie_header(provider)
    if ck:
        h["cookie"] = ck
    tok = _bearer(provider)
    if tok:
        h["authorization"] = f"Bearer {tok}"
    return h


# ── Сериализация диалога в один промпт ────────────────────────────────

def _flatten_prompt(messages: list[dict[str, Any]]) -> str:
    """Веб-чат принимает один текст: историю приклеиваем к вопросу."""
    sys_parts = [str(m.get("content") or "") for m in messages
                 if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    convo = [m for m in convo if (m.get("content") or "").strip()]
    last_user = ""
    if convo and convo[-1].get("role") == "user":
        last_user = str(convo[-1].get("content") or "")
        convo = convo[:-1]

    parts: list[str] = []
    if sys_parts:
        parts.append("\n".join(p for p in sys_parts if p.strip()))
    if convo:
        lines = []
        for m in convo[-10:]:
            who = "Пользователь" if m.get("role") == "user" else "Ассистент"
            text = str(m.get("content") or "").replace("\n", " ")[:400]
            lines.append(f"{who}: {text}")
        parts.append("История диалога:\n" + "\n".join(lines))
    parts.append(last_user or "Продолжай.")
    return "\n\n".join(p for p in parts if p.strip())


# ── DeepSeek PoW (DeepSeekHashV1, wasm через node) ────────────────────

async def _deepseek_pow_header(client: httpx.AsyncClient,
                               headers: dict[str, str]) -> str:
    """challenge → решить nonce (node+wasm) → base64-заголовок x-ds-pow-response."""
    r = await client.post(
        f"{DEEPSEEK_BASE}/api/v0/chat/create_pow_challenge",
        headers=headers, json={"target_path": "/api/v0/chat/completion"},
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"DeepSeek PoW challenge HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    ch = ((data.get("data") or {}).get("biz_data") or {}).get("challenge") or {}
    if not ch.get("challenge"):
        raise RuntimeError(f"DeepSeek PoW: нет challenge: {str(data)[:200]}")
    if not (os.path.isfile(_POW_SOLVER_CJS) and os.path.isfile(_POW_WASM)):
        raise RuntimeError(
            "DeepSeek PoW: нет src/pow_solver.cjs или data/pow/sha3_wasm_bg.wasm")
    proc = await asyncio.create_subprocess_exec(
        "node", _POW_SOLVER_CJS,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(json.dumps(ch).encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(
            f"DeepSeek PoW solver: {err.decode('utf-8', 'replace')[:200]}")
    try:
        answer = json.loads(out.decode("utf-8")).get("answer")
    except json.JSONDecodeError:
        raise RuntimeError(f"DeepSeek PoW solver: плохой вывод: {out[:120]!r}")
    if answer is None:
        raise RuntimeError("DeepSeek PoW: nonce не найден (difficulty?)")
    payload = {
        "algorithm": ch["algorithm"], "challenge": ch["challenge"],
        "salt": ch["salt"], "answer": answer, "signature": ch["signature"],
        "target_path": ch["target_path"],
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode()


# ── Парсер SSE-дельт (устойчивый к форматам) ──────────────────────────

def _extract_piece(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("content", "text"):
        v = obj.get(key)
        if isinstance(v, str) and v:
            return v
    v = obj.get("v")
    if isinstance(v, dict):
        for key in ("response", "content", "text", "delta"):
            val = v.get(key)
            if isinstance(val, str) and val:
                return val
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
        c = delta.get("content")
        if isinstance(c, str) and c:
            return c
    return ""


class _DeepseekStreamState:
    """JSON-patch стрим DeepSeek 2026: снапшот фрагментов + патчи.

    События: {"v":{"response":{"fragments":[...]}}} — снапшот;
    {"v":"строка"} — дельта в последний фрагмент;
    {"p":"response/fragments","o":"APPEND","v":[{...}]} — новый фрагмент;
    {"p":"response/fragments/-1/content","o":"APPEND|SET","v":"строка"};
    {"p":"response/status"|"response","o":"SET|BATCH"} — служебные (мимо).
    Итог = конкатенация фрагментов типа RESPONSE; THINK — рассуждения.
    """
    def __init__(self) -> None:
        self.answer: list[str] = []
        self.reasoning: list[str] = []
        self._cur = "RESPONSE"

    def _push(self, s: str) -> None:
        (self.reasoning if self._cur == "THINK" else self.answer).append(s)

    def feed(self, obj: Any) -> list[tuple[str, bool]]:
        """→ [(куска, это_рассуждение)]; пусто — не текст/служебное."""
        if not isinstance(obj, dict):
            return []
        p, v = obj.get("p"), obj.get("v")
        if isinstance(p, str):
            if p.endswith("/content") and isinstance(v, str) and v:
                self._push(v)
                return [(v, self._cur == "THINK")]
            if p == "response/fragments" and isinstance(v, list):
                out: list[tuple[str, bool]] = []
                for f in v:
                    if not isinstance(f, dict):
                        continue
                    self._cur = str(f.get("type") or "RESPONSE")
                    c = f.get("content")
                    if isinstance(c, str) and c:
                        self._push(c)
                        out.append((c, self._cur == "THINK"))
                return out
            return []
        if isinstance(v, dict):
            resp = v.get("response")
            frags = resp.get("fragments") if isinstance(resp, dict) else None
            out = []
            if isinstance(frags, list):
                for f in frags:
                    if not isinstance(f, dict):
                        continue
                    self._cur = str(f.get("type") or "RESPONSE")
                    c = f.get("content")
                    if isinstance(c, str) and c:
                        self._push(c)
                        if self._cur != "THINK":
                            out.append((c, False))
            return out
        if isinstance(v, str) and v:
            self._push(v)
            return [(v, self._cur == "THINK")]
        return []

    def answer_text(self) -> str:
        return "".join(self.answer).strip()


async def _stream_sse(
    client: httpx.AsyncClient, method: str, url: str, headers: dict,
    body: dict | None, on_token: TokenCallback | None,
    stream_started: dict | None, timeout: float = 180.0,
    label: str = "", state: _DeepseekStreamState | None = None,
    on_reasoning: TokenCallback | None = None,
) -> str:
    """POST с SSE-ответом; текст по кусочно уходит в on_token.

    state задан — разбор через JSON-patch парсер DeepSeek (итог из state);
    без state — универсальный _extract_piece (qwen и др.).
    """
    chunks: list[str] = []
    async with client.stream(
        method, url, headers=headers, json=body, timeout=timeout,
    ) as resp:
        if resp.status_code >= 400:
            err = await resp.aread()
            text = err.decode("utf-8", "replace")[:300]
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"{label} HTTP {resp.status_code}: куки/токен отклонены "
                    f"— протухли или не те. Обновите: расширение Bridge → "
                    f"ключ (DeepSeek/Qwen) → войдите в чат заново; статус: "
                    f"вкладка «Мост» (/api/bridge/cookies/status). "
                    f"Сайт ответил: {text}"
                )
            raise RuntimeError(f"{label} HTTP {resp.status_code}: {text}")
        ct = resp.headers.get("content-type", "")
        if "event-stream" not in ct and "text/plain" not in ct:
            raw = await resp.aread()
            text = raw.decode("utf-8", "replace")[:300]
            raise RuntimeError(
                f"{label}: ожидался SSE, пришёл {ct or 'пустой CT'}: {text}")
        logger.debug("SSE[%s] stream opened, ct=%s", label, ct)
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if state is not None:
                pieces = state.feed(obj)
            else:
                piece = _extract_piece(obj)
                pieces = [(piece, False)] if piece else []
            for piece, thinking in pieces:
                if not piece:
                    continue
                if thinking:
                    if on_reasoning is None:
                        continue
                    cb, tag = on_reasoning, "reasoning"
                else:
                    if state is None:
                        chunks.append(piece)
                    cb, tag = on_token, "token"
                if cb:
                    if stream_started is not None:
                        stream_started["v"] = True
                    try:
                        await cb(piece)
                    except Exception as e:
                        logger.warning("on_%s: %s", tag, e)
    return state.answer_text() if state is not None else "".join(chunks).strip()


# ── Провайдеры ────────────────────────────────────────────────────────

async def _deepseek_chat(
    messages: list[dict[str, Any]], on_token: TokenCallback | None,
    stream_started: dict | None, thinking: bool = False,
    on_reasoning: TokenCallback | None = None,
) -> str:
    prompt = _flatten_prompt(messages)
    headers = _base_headers("deepseek")
    async with httpx.AsyncClient(
        timeout=180.0, follow_redirects=True,
    ) as client:
        r = await client.post(
            f"{DEEPSEEK_BASE}/api/v0/chat_session/create",
            headers=headers, json={},
        )
        if r.status_code >= 400:
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"DeepSeek HTTP {r.status_code}: куки/токен отклонены "
                    f"— протухли или не те. Обновите: расширение Bridge → "
                    f"ключ DeepSeek → войдите в чат заново; статус: "
                    f"вкладка «Мост» (/api/bridge/cookies/status). "
                    f"Сайт ответил: {r.text[:300]}"
                )
            raise RuntimeError(
                f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"DeepSeek API error: {data}")
        bd = data.get("data", {}).get("biz_data", {}) or {}
        # старый формат: biz_data.chat_session.id; новый (2026): biz_data.id
        sid = bd.get("id") or (bd.get("chat_session") or {}).get("id")
        if not sid:
            raise RuntimeError("DeepSeek: нет chat_session id")
        # completion требует x-ds-pow-response (DeepSeekHashV1, ~0.1с)
        pow_hdr = await _deepseek_pow_header(client, headers)
        headers = dict(headers)
        headers["x-ds-pow-response"] = pow_hdr
        body = {
            "chat_session_id": sid,
            "parent_message_id": None,
            "model_type": "default",
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": bool(thinking),
            "search_enabled": False,
            "action": None,
            "preempt": False,
        }
        state = _DeepseekStreamState()
        return await _stream_sse(
            client, "POST", f"{DEEPSEEK_BASE}/api/v0/chat/completion",
            headers, body, on_token, stream_started, label="deepseek",
            state=state, on_reasoning=on_reasoning,
        )


def _qwen_model() -> str:
    """Модель Qwen web (env QWEN_WEB_MODEL; дефолт — как в веб-клиенте)."""
    return os.getenv("QWEN_WEB_MODEL", "").strip() or "qwen3.7-plus"


_QWEN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 YaBrowser/26.8.0.0 Safari/537.36"
)


def _qwen_v2_headers(referer: str, accept: str) -> dict[str, str]:
    """Заголовки chat.qwen.ai v2 (Aliyun WAF: нужны sec-*/timezone/x-request-id;
    bx-ua/bx-umidtoken НЕ обязательны — проверено)."""
    h = {
        "accept": accept,
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {_bearer('qwen')}",
        "content-type": "application/json",
        "origin": QWEN_BASE,
        "referer": referer,
        "sec-ch-ua": ('"Not;A=Brand";v="8", "Chromium";v="150", '
                      '"YaBrowser";v="26.8", "Browser";v="26.8"'),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "source": "web",
        "timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT+0800",
                                  time.gmtime(time.time() + 8 * 3600)),
        "user-agent": _QWEN_UA,
        "version": "0.3.17",
        "x-request-id": str(uuid.uuid4()),
    }
    ck = _cookie_header("qwen")
    if ck:
        h["cookie"] = ck
    return h


class _QwenStreamState:
    """SSE chat.qwen.ai v2: choices[0].delta.content с phase.

    phase "answer" → итог; "thinking_summary"/"thinking" → рассуждения;
    без phase (старые форматы) → итог (совместимость)."""
    def __init__(self) -> None:
        self.answer: list[str] = []
        self.reasoning: list[str] = []

    def feed(self, obj: Any) -> list[tuple[str, bool]]:
        if not isinstance(obj, dict):
            return []
        choices = obj.get("choices")
        if not (isinstance(choices, list) and choices
                and isinstance(choices[0], dict)):
            return []
        delta = choices[0].get("delta") or {}
        ct = delta.get("content")
        if not (isinstance(ct, str) and ct):
            return []
        phase = str(delta.get("phase") or "answer")
        thinking = phase in ("thinking", "thinking_summary")
        (self.reasoning if thinking else self.answer).append(ct)
        return [(ct, thinking)]

    def answer_text(self) -> str:
        return "".join(self.answer).strip()


async def _qwen_chat(
    messages: list[dict[str, Any]], on_token: TokenCallback | None,
    stream_started: dict | None, on_reasoning: TokenCallback | None = None,
) -> str:
    """Qwen web v2: chats/new → chat/completions?chat_id=... (SSE).

    Токен access живёт ~15 минут после autofill; при истечении сайт
    отвечает success=false "Token has expired" (или 401) — тогда нужен
    повторный autofill (вкладка Bridge).
    """
    prompt = _flatten_prompt(messages)
    model = _qwen_model()
    async with httpx.AsyncClient(
        timeout=180.0, follow_redirects=True,
    ) as client:
        r = await client.post(
            f"{QWEN_BASE}/api/v2/chats/new",
            headers=_qwen_v2_headers(f"{QWEN_BASE}/c/new-chat",
                                     "application/json, text/plain, */*"),
            json={
                "chatId": "", "models": [model], "project_id": "",
                "timestamp": int(time.time() * 1000),
                "chat_type": "t2t", "chat_mode": "normal",
            },
        )
        if r.status_code >= 400:
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"Qwen HTTP {r.status_code}: куки/токен отклонены "
                    f"— протухли (access token живёт ~15 мин). Обновите: "
                    f"расширение Bridge → ключ Qwen → войдите в чат заново; "
                    f"статус: вкладка «Мост» (/api/bridge/cookies/status). "
                    f"Сайт ответил: {r.text[:200]}"
                )
            raise RuntimeError(f"Qwen HTTP {r.status_code}: {r.text[:200]}")
        try:
            d = r.json()
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Qwen: не-JSON ответ (WAF?): {r.text[:200]}")
        if not d.get("success"):
            detail = str(d.get("data") or d)[:200]
            raise RuntimeError(
                f"Qwen chats/new error: {detail} — вероятно, access token "
                f"истёк (~15 мин), обновите куки через расширение Bridge")
        chat_id = ((d.get("data") or {}).get("id")
                   or (d.get("data") or {}).get("chat_id") or "")
        if not chat_id:
            raise RuntimeError(f"Qwen: нет chat_id: {str(d)[:200]}")
        h2 = _qwen_v2_headers(f"{QWEN_BASE}/c/{chat_id}", "application/json")
        h2["x-accel-buffering"] = "no"
        body = {
            "stream": True, "version": "2.1", "incremental_output": True,
            "chatId": chat_id, "parentId": "", "chat_id": chat_id,
            "chat_mode": "normal", "model": model, "parent_id": None,
            "messages": [{
                "id": None, "fid": str(uuid.uuid4()), "parentId": None,
                "childrenIds": [], "role": "user", "content": prompt,
                "user_action": "chat", "files": [],
                "timestamp": int(time.time()), "models": [model],
                "model": "", "chat_type": "t2t",
                "feature_config": {
                    "thinking_enabled": False, "output_schema": "phase",
                    "research_mode": "normal", "auto_thinking": False,
                    "thinking_mode": "Auto", "thinking_format": "summary",
                    "auto_search": False},
                "extra": {"meta": {"subChatType": "t2t"}},
                "sub_chat_type": "t2t", "parent_id": None,
            }],
            "timestamp": int(time.time()),
        }
        state = _QwenStreamState()
        return await _stream_sse(
            client, "POST",
            f"{QWEN_BASE}/api/v2/chat/completions?chat_id={chat_id}",
            h2, body, on_token, stream_started, label="qwen",
            state=state, on_reasoning=on_reasoning,
        )



# ── Автообновление кук при истечении (Фаза 2) ────────────────────────

_AUTH_ERR_MARKERS = (
    "token has expired", "unauthorized", "куки/токен отклонены",
    "authorization failed", "missing token", "http 401", "http 403",
    "access token",
)


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(m in low for m in _AUTH_ERR_MARKERS)


async def _with_cookie_refresh(provider: str, call: Callable[[], Awaitable[str]]) -> str:
    """Один повтор после autofill, если ошибка похожа на истёкшие куки."""
    try:
        return await call()
    except RuntimeError as e:
        if not _is_auth_error(str(e)):
            raise
        if os.getenv("COOKIE_AUTOFILL", "1").strip() in ("0", "false", ""):
            raise
        import asyncio as _asyncio
        from src.cookie_autofill import autofill
        logger.warning("%s: похоже на истёкшие куки — autofill (%s)",
                       provider, str(e)[:120])
        try:
            await _asyncio.to_thread(autofill, provider, True)
        except Exception as ae:
            logger.warning("autofill %s не удался: %s", provider, ae)
            raise e from ae
        return await call()


async def web_chat(
    provider: str, messages: list[dict[str, Any]],
    on_token: TokenCallback | None = None,
    stream_started: dict | None = None,
    thinking: bool = False,
    on_reasoning: TokenCallback | None = None,
) -> dict[str, Any]:
    """Ответ веб-чата; форма результата совпадает с LLMClient.chat()."""
    if provider == "deepseek":
        if not web_cookie_available("deepseek"):
            raise RuntimeError(
                "Нет кук DeepSeek — откройте страницу чата в расширении Bridge")

        async def _call():
            return await _deepseek_chat(
                messages, on_token, stream_started, thinking,
                on_reasoning=on_reasoning)

        text = await _with_cookie_refresh("deepseek", _call)
    elif provider == "qwen":
        if not web_cookie_available("qwen"):
            raise RuntimeError(
                "Нет кук Qwen — откройте страницу чата в расширении Bridge")

        async def _call():
            return await _qwen_chat(messages, on_token, stream_started,
                                    on_reasoning=on_reasoning)

        text = await _with_cookie_refresh("qwen", _call)
    else:
        raise RuntimeError(f"Неизвестный веб-провайдер: {provider}")
    return {
        "role": "assistant",
        "content": text or "(пустой ответ веб-чата)",
        "chunks": [],
        "streamed": bool(on_token is not None),
        "web_provider": provider,
    }
