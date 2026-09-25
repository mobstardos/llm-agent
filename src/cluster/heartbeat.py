"""Периодический heartbeat инстанса в Redis."""
from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)


async def heartbeat_loop(bus, config, get_stats):
    """Регулярно публикует информацию о текущем инстансе."""
    while True:
        try:
            stats = get_stats() if get_stats else {}
            await bus.register_instance({
                "instance_id": config.instance_id,
                "pid": os.getpid(),
                "started_at": time.time(),
                "stats": stats,
            })
        except Exception as e:
            logger.debug("Heartbeat: %s", e)
        await asyncio.sleep(config.heartbeat_interval_seconds)
