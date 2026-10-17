# -*- coding: utf-8 -*-
"""Пакет роутеров FastAPI.

Рекомендация 1 (Sprint 1.A — подготовка).

Каждый модуль здесь экспортирует `router = APIRouter(...)`. Регистрация
в src/app.py через `app.include_router(<mod>.router, ...)`.

Sprint 1.A: только static, cache, features.
Sprint 1.B (план): plans, models, sessions, policies, registry, database,
                   analytics, graph, search, backup, chats, project.
Sprint 1.C (план): ws/chat.py (WebSocket handler, ~480 строк).
"""
