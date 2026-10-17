# -*- coding: utf-8 -*-
"""Запуск прокси: python -m src.mcp_servers.onec_qa
(эквивалент python -m src.mcp_servers.onec_qa.server)."""
from .server import main

if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
