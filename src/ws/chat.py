# -*- coding: utf-8 -*-
"""WebSocket handler для веб-чата (/ws endpoint).

Sprint 1.C: вынесено из src/main.py (строки 2161-2638, ~480 строк).

Полный цикл чата:
  1. Принять WS, инициализировать сессию журнала (мягко)
  2. Запустить reader() — async queue для входящих сообщений
  3. Главный цикл:
     - hello: восстановление сессии (session_id из localStorage)
     - approval_response: резолвит future (HITL approval gate)
     - edit_message: правка сообщения пользователя + перезапуск
     - обычный запрос: Intent Layer → Supervisor OR LLM-роутинг → handle
  4. Cleanup: ws_session_end + reader_task.cancel()

Что осталось в main.py после Stage C+D:
  - 5 строк: импорт app, запуск uvicorn (см. patches/0001-stage-D-main-cleanup.patch)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.attachments import build_attachments_block
from src.file_state import FileState
from src.journal.integration import (
    ws_session_end, ws_session_start,
)
from src.memory.facade import Memory
from src.orchestrator import Orchestrator
from src.policies import PolicyStore
from src.state import state

logger = logging.getLogger(__name__)

ws_router = APIRouter()


def _prompt_with_workspace(prompt: str | None) -> str | None:
    """Добавляет к orchestrator-промпту строку о рабочей папке (Фаза 3).

    Если строка о папке уже есть в prompt (registry snapshot) — не дублирует.
    """
    try:
        from src.workspace import workspace_prompt_line
        line = workspace_prompt_line()
    except Exception:
        return prompt
    if not line or (prompt and "Рабочая папка проекта:" in prompt):
        return prompt
    if prompt:
        return prompt + "\n\n" + line
    return line


@ws_router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Главный WS handler — цикл чата с LLM через агентов.

    State-доступ через `state.X` (AppState из Sprint 1.A) вместо
    legacy dict-доступа. Обратная совместимость НЕ нужна — после
    Stage D в main.py не остаётся WS-кода.
    """
    # ── Безопасность v2: API-key + origin-check ДО accept() ──────
    # Без настроенного LLM_AGENT_API_KEY — открытый доступ (локальная
    # разработка); origin localhost/127.0.0.1 разрешён всегда.
    from src.security import ws_authorized
    ok, reason = ws_authorized(
        headers=dict(websocket.headers),
        query_params=dict(websocket.query_params),
        cookies=dict(websocket.cookies),
    )
    if not ok:
        await websocket.close(code=4401, reason=reason)
        return

    await websocket.accept()

    # Лениво: src.app импортирует ws_router — цикл при import-first
    from src.app import SUPERVISOR_ENABLED

    # ── Журнал: мягкая сессия (no-op если журнал выключен) ────────
    ws_journal_session = ws_session_start()
    try:
        from src.journal.recorder import _cv_session as _jr_cv_session
        _jr_cv_session.set(ws_journal_session)
    except Exception:
        pass

    # ── State — через AppState (Sprint 1.A) ────────────────────────
    policies: PolicyStore | None = state.policies
    memory: Memory | None = state.memory
    orchestrator: Orchestrator | None = state.orchestrator

    # ── Внутренние очереди ──────────────────────────────────────────
    incoming: asyncio.Queue = asyncio.Queue()
    pending_approvals: dict[str, asyncio.Future] = {}

    # ── Lazy imports для диалога ───────────────────────────────────
    from src.llm_errors import friendly_llm_error
    from src.supervisor.intents import (
        build_agent_cards,
        classify as classify_intent,
    )
    from src.supervisor.session import ConversationSession

    session = ConversationSession(dump_dir=Path("data/sessions"))

    def make_session(sid: str = "") -> ConversationSession:
        """Создаёт сессию с заданным id и подхватывает дамп с диска."""
        s = ConversationSession(session_id=sid or "", dump_dir=Path("data/sessions"))
        s.load()
        return s

    async def restore_hello() -> None:
        """Ответ на hello: id сессии + план + история."""
        last_plan = None
        reg = state.plans
        if reg is not None:
            lp = reg.for_session(session.id)
            if lp is not None:
                from src.supervisor.plans import view_for_client
                last_plan = view_for_client(lp)
        try:
            await websocket.send_json({
                "type": "session_restored", "session_id": session.id,
                "history": len(session), "plan": last_plan,
                "title": session.title,
                "messages": session.messages_view(100),
            })
        except Exception:
            pass

    async def direct_chat_answer(query_text: str) -> str:
        """Простой чат с LLM без агентов (болтовня)."""
        llm = state.llm
        if llm is None:
            return "LLM не настроен — выполните: python first_run.py --reconfigure-ai"
        try:
            history_text = session.history_for_router(6, exclude_last=True)
            content = (f"История диалога:\n{history_text}\n\n{query_text}"
                       if history_text else query_text)
            msg = await llm.chat([
                {"role": "system",
                 "content": "Ты — дружелюбный ассистент мультиагентной системы. "
                            "Отвечай кратко и по-русски."},
                {"role": "user", "content": content},
            ], model=model)
            return (msg.get("content") or "").strip() or "Чем помочь?"
        except Exception as e:
            logger.warning("direct_chat failed: %s", e)
            hint = friendly_llm_error(e)
            return hint if hint else f"Провайдер недоступен: {e}"

    async def send_done() -> None:
        """Финальный done — client мог уже отключиться."""
        try:
            await websocket.send_json({"type": "done"})
        except Exception:
            pass

    async def reader():
        """Читает входящие WS-сообщения и кладёт в queue."""
        nonlocal session
        try:
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type", "")
                if mtype == "approval_response":
                    rid = msg.get("id")
                    fut = pending_approvals.get(rid)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif mtype == "hello":
                    sid = str(msg.get("session_id") or "").strip()
                    if sid == "__new__":
                        session = ConversationSession(dump_dir=Path("data/sessions"))
                        sid = session.id
                    elif sid and sid != session.id:
                        session = make_session(sid)
                    await restore_hello()
                elif mtype == "edit_message":
                    sid = str(msg.get("session_id") or "").strip()
                    text = str(msg.get("text") or "").strip()
                    try:
                        idx = int(msg.get("index", -1))
                    except Exception:
                        idx = -1
                    if sid == "__new__":
                        session = ConversationSession(dump_dir=Path("data/sessions"))
                        sid = session.id
                    elif sid and sid != session.id:
                        session = make_session(sid)
                    fixed = session.edit_user(idx, text)
                    if fixed < 0:
                        await websocket.send_json({
                            "type": "error",
                            "text": "Правка не удалась: неверный индекс или пустой текст",
                        })
                        continue
                    await websocket.send_json({
                        "type": "session_edited", "index": fixed,
                        "messages": session.messages_view(100),
                    })
                    await incoming.put({
                        "query": text, "model": msg.get("model"),
                        "session_id": session.id, "_regen": True,
                    })
                else:
                    await incoming.put(msg)
        except WebSocketDisconnect:
            await incoming.put(None)
        except Exception:
            logger.exception("WS reader")
            await incoming.put(None)

    async def approval_handler(request: dict) -> bool:
        """HITL approval gate — отправляет запрос в UI, ждёт ответа."""
        tool = request.get("tool", "")
        args = request.get("arguments", {}) or {}
        if policies is not None:
            cached = policies.check(tool, args)
            if cached is not None:
                return cached

        rid = str(uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        pending_approvals[rid] = fut

        await websocket.send_json({
            "type": "approval_request",
            "id": rid, "tool": tool, "arguments": args,
            "reason": request.get("reason", ""),
            "has_path": bool(args.get("path")),
            "path": args.get("path"),
        })

        try:
            response = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            pending_approvals.pop(rid, None)

        approved = bool(response.get("approved"))
        remember = response.get("remember", "once")
        decision = "allow" if approved else "deny"

        if memory:
            if approved:
                memory.record_approval(tool, args.get("path"))
            else:
                memory.record_rejection(tool, args.get("path"))

        if remember != "once" and policies is not None:
            try:
                if remember == "tool":
                    policies.add(tool, "tool", decision)
                elif remember == "path":
                    p = args.get("path")
                    if p:
                        policies.add(tool, "path", decision,
                                     str(p).replace("\\", "/"))
                elif remember == "folder":
                    p = args.get("path")
                    if p:
                        parent = str(Path(str(p)).parent).replace("\\", "/")
                        pat = "**" if parent in (".", "") else parent + "/**"
                        policies.add(tool, "path", decision, pat)
            except Exception as e:
                logger.warning("Policy save: %s", e)
        return approved

    async def plan_approval_handler(plan_dict: dict) -> bool:
        """Approval gate для Supervisor-плана (V2 §3.7)."""
        rid = str(uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        pending_approvals[rid] = fut
        try:
            await websocket.send_json({
                "type": "plan_approval", "id": rid, "plan": plan_dict,
            })
        except Exception:
            pending_approvals.pop(rid, None)
            return False
        try:
            response = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            pending_approvals.pop(rid, None)
        return bool(response.get("approved"))

    async def on_token(token: str) -> None:
        try:
            await websocket.send_json({"type": "token", "token": token})
        except Exception:
            pass

    async def on_reasoning(text: str) -> None:
        """Task 24-d: ход мыслей reasoning-моделей (DeepSeek-R1/QwQ)."""
        try:
            await websocket.send_json({"type": "reasoning", "text": text})
        except Exception:
            pass

    async def on_step(agent_name: str, step: int) -> None:
        try:
            await websocket.send_json({
                "type": "step_start", "agent": agent_name, "step": step,
            })
        except Exception:
            pass

    # ─── Запуск reader и регистрация в ws_clients ──────────────────
    reader_task = asyncio.create_task(reader())

    ws_clients = state.ws_clients
    if ws_clients is not None:
        ws_clients.add(websocket)

    try:
        while True:
            payload = await incoming.get()
            if payload is None:
                break
            query = payload.get("query", "").strip()
            model = payload.get("model")
            if not query:
                continue

            # ── Вложения файлов: манифест + инлайн ≤32 КБ ─────────
            # Intent-слой и smalltalk получают чистый query; блок
            # добавляется к query ПОСЛЕ классификации интента.
            files_block = ""
            try:
                files_block = build_attachments_block(
                    payload.get("attachments") or [])
            except Exception:
                logger.exception("attachments block failed")

            # ── Session restore (без hello) ────────────────────────
            req_sid = str(payload.get("session_id") or "").strip()
            if req_sid == "__new__":
                session = ConversationSession(dump_dir=Path("data/sessions"))
            elif req_sid and req_sid != session.id:
                session = make_session(req_sid)

            # ── Добавить user-сообщение в историю ─────────────────
            # _regen: edit_user уже переписал сообщение — не дублируем
            if not payload.get("_regen"):
                session.add_user(query)

            # ── Сохранить выбор модели в runtime.yaml ──────────────
            reg = state.registry
            if model and reg is not None:
                try:
                    if reg.runtime.get_model_pref() != model:
                        reg.runtime.set_model_pref(model, written_by="ws")
                except Exception:
                    pass

            # ── Local model VRAM window (Task 24-c) ───────────────
            try:
                from src.llm_providers import is_local_model
                if model and is_local_model(model):
                    state.local_chat_active_until = time.time() + 120
            except Exception:
                pass

            await websocket.send_json({"type": "start"})
            await websocket.send_json(
                {"type": "status", "text": "Маршрутизация..."}
            )

            # ─── Intent Layer (детерминированный быстрый путь) ────
            intent = None
            try:
                cards = build_agent_cards(
                    reg.snapshot if reg else None,
                    active_ids=set(orchestrator.runtime.agents.keys()) if orchestrator else set(),
                )
                intent = classify_intent(query, session, cards)
            except Exception:
                logger.exception("Intent layer failed — fallback к LLM-роутингу")

            _ra_suggest = (list(intent.agents) if intent is not None
                           and not intent.needs_llm_routing else [])

            # Вложения — в query ПОСЛЕ интента (intent/smalltalk чистые)
            if files_block:
                query = f"{query}\n\n{files_block}"

            # ─── Smalltalk: ответ без агентов ─────────────────────
            # (с вложениями быстрый путь не берём — файл должен увидеть
            #  модель/агент, а не «привет» из малого разговора)
            if (intent is not None and intent.source == "smalltalk"
                    and not files_block):
                from src import route_analytics as _ra
                _ra.log_decision(
                    query=query, source="intent.smalltalk", agents=[],
                    reason=intent.reason, session_id=session.id or "",
                    confidence=intent.confidence,
                )
                answer = await direct_chat_answer(query)
                await websocket.send_json({
                    "type": "route", "agents": [],
                    "reason": f"[Intent:{intent.source}] {intent.reason}",
                })
                await websocket.send_json({
                    "type": "message", "role": "assistant",
                    "content": answer,
                })
                session.add_assistant(answer)
                await send_done()
                continue

            # ─── Supervisor: Plan → Execute → Observe → Re-plan ────
            if SUPERVISOR_ENABLED and orchestrator is not None:
                from src.supervisor.supervisor import Supervisor
                supervisor = Supervisor(orchestrator)

                async def emit(event: dict) -> None:
                    try:
                        await websocket.send_json(event)
                    except Exception:
                        pass

                file_state_sv: FileState | None = state.file_state
                await supervisor.run(
                    query, model=model,
                    session=session,
                    suggested_agents=(list(intent.agents)
                                      if intent is not None
                                      and not intent.needs_llm_routing else None),
                    intent_source=(intent.source if intent is not None else "low_confidence"),
                    intent_reason=(intent.reason if intent is not None else ""),
                    llm_client=state.llm,
                    mcp_manager=state.mcp,
                    memory=memory,
                    file_state=file_state_sv,
                    emit=emit,
                    approval_handler=approval_handler,
                    plan_approval_handler=plan_approval_handler,
                    plan_registry=state.plans,
                    fallback_answer=direct_chat_answer,
                )
                await send_done()
                continue

            # ─── Fast path: агент выбран без LLM ──────────────────
            if intent is not None and not intent.needs_llm_routing:
                route = {"agents": list(intent.agents),
                         "reason": f"[Intent:{intent.source}] {intent.reason}"}
                try:
                    from src import route_analytics as _ra
                    _ra.log_decision(
                        query=query, source=f"intent.{intent.source}",
                        agents=route["agents"], reason=intent.reason,
                        session_id=session.id or "",
                        confidence=intent.confidence,
                    )
                except Exception:
                    pass
            else:
                # ─── LLM-роутинг (с историей диалога, Этап 1) ───────
                if orchestrator is None:
                    await websocket.send_json({"type": "error", "text": "orchestrator not ready"})
                    continue
                route = await orchestrator.route(
                    query, model=model,
                    prompt=_prompt_with_workspace(
                        reg.snapshot.orchestrator_prompt
                        if reg and reg.snapshot else None),
                    history=session.history_for_router(
                        exclude_last=True) or None,
                )

            await websocket.send_json({
                "type": "route", "agents": route["agents"],
                "reason": route.get("reason", ""),
            })

            if not route["agents"]:
                # Фолбэк: агент не нужен / роутер ошибся — отвечаем сами,
                # чтобы чат не умирал пустым «Нет подходящего агента»
                try:
                    from src import route_analytics as _ra
                    _ra.log_decision(
                        query=query, source="fallback.direct_chat", agents=[],
                        reason=route.get("reason", "") or "нет агентов",
                        session_id=session.id or "",
                    )
                except Exception:
                    pass
                answer = await direct_chat_answer(query)
                await websocket.send_json({
                    "type": "message", "role": "assistant",
                    "content": answer,
                })
                session.add_assistant(answer)
                await send_done()
                continue

            # ─── Execute — orchestrator.handle() ────────────────────
            file_state: FileState | None = state.file_state
            result = await orchestrator.handle(
                query, model=model,
                agents=route["agents"],
                system_prompt=_prompt_with_workspace(
                    reg.snapshot.orchestrator_prompt
                    if reg and reg.snapshot else None),
                llm_client=state.llm,
                mcp_manager=state.mcp,
                memory=memory,
                file_state=file_state,
                on_token=on_token,
                on_reasoning=on_reasoning,
                on_step=on_step,
                approval_handler=approval_handler,
            )

            # ─── Стримим результат клиенту ─────────────────────────
            for r in result["results"]:
                if memory:
                    memory.record_assistant_message(r.get("content", ""), tokens=0)
                session.add_assistant(r.get("content", ""), agent=r.get("agent", ""))
                await websocket.send_json({
                    "type": "message", "role": "assistant",
                    "agent": r["agent"], "content": r["content"],
                    "steps": r.get("steps", []),
                    "tool_calls": r.get("tool_calls", 0),
                })

            session.set_last_agents(route["agents"])

            # ─── Analytics: итог handle()-пути ────────────────────
            try:
                from src import route_analytics as _ra
                _ra.log_outcome(
                    session_id=session.id or "",
                    agents=[{"agent": r.get("agent", ""),
                             "success": bool(r.get("success")),
                             **({"error": r.get("error")}
                                if r.get("error") else {})}
                            for r in result.get("results", [])],
                    success=bool(result.get("success")),
                )
            except Exception:
                pass

            await send_done()
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.exception("WS error")
        try:
            await websocket.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass
    finally:
        ws_session_end(ws_journal_session)
        reader_task.cancel()
        ws_clients = state.ws_clients
        if ws_clients is not None:
            ws_clients.discard(websocket)
