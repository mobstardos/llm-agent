"""WebSocket bridge через Redis для multi-instance."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class WsBridge:
    """Пересылает сообщения WS между инстансами через Redis.

    Использование:
      - Пользователь подключён к Instance A.
      - Задача обрабатывается на Instance B.
      - B публикует ответ в channel_ws:session_id.
      - A подписан → пересылает по WS клиенту.
    """

    def __init__(self, bus, config):
        self.bus = bus
        self.cfg = config
        self._sessions: dict[str, Any] = {}

    async def register_session(self, session_id: str, websocket):
        """Регистрирует WS-сессию на этом инстансе."""
        channel = f"{self.cfg.channel_ws_prefix}{session_id}"
        self._sessions[session_id] = websocket

        async def _forward(payload: dict):
            try:
                await websocket.send_json(payload)
            except Exception as e:
                logger.debug("Forward to session %s: %s", session_id, e)

        await self.bus.subscribe(channel, _forward)

    async def publish_to_session(
        self, session_id: str, payload: dict,
    ) -> None:
        channel = f"{self.cfg.channel_ws_prefix}{session_id}"
        await self.bus.publish(channel, payload)

    def unregister_session(self, session_id: str):
        self._sessions.pop(session_id, None)
