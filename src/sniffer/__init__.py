# -*- coding: utf-8 -*-
"""
sniffer — вендоренная копия UniversalSniffer v3.1 внутри llm-agent.

Универсальный TCP-прокси сниффер: перехватывает трафик между клиентами и
сервером (прозрачное проксирование), разбирает протоколы плагинами
(thrift, remote_server, modbus, json_http, hexdump), пишет JSONL / SQLite /
PCAP / CSV и поднимает тревоги по правилам. Только стандартная библиотека
Python — внешних зависимостей нет.

Запуск из корня проекта:
    python -m src.sniffer --ports 3003:3004,10010:10011
    python -m src.sniffer --config src/sniffer/config.json
Самопроверка (локальный эхо-сервер + движок, без внешней сети):
    python -m src.sniffer.selftest

Веб-панель по умолчанию: http://127.0.0.1:9500/ (HTTP API: /api/status,
/api/stats, /api/series, /api/sessions, /api/packets, /api/packet/<id>,
/api/config, /api/alerts, /api/export/csv, POST /api/reload).
"""

__version__ = "3.1.0"

__all__ = ["__version__"]
