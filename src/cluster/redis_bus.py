"""Redis Pub/Sub для событий между инстансами."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class RedisBus:
    """Публикация и подписка на события."""

    def __init__(self, config):
        self.cfg = config
        self._redis = None
        self._pubsub = None
        self._listeners: dict[str, list[Callable]] = {}
        self._task: asyncio.Task | None = None

    async def connect(self):
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.warning("redis.asyncio не установлен")
            return False
        try:
            self._redis = aioredis.from_url(
                self.cfg.redis_url, decode_responses=True,
            )
            await self._redis.ping()
            logger.info("Redis подключён: %s", self.cfg.redis_url)
            return True
        except Exception as e:
            logger.warning("Redis connect: %s", e)
            return False

    async def publish(self, channel: str, payload: dict) -> None:
        if not self._redis:
            return
        try:
            await self._redis.publish(
                channel,
                json.dumps(payload, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.debug("Publish %s: %s", channel, e)

    async def subscribe(
        self, channel: str, callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        if channel not in self._listeners:
            self._listeners[channel] = []
        self._listeners[channel].append(callback)

        if self._pubsub is None and self._redis:
            self._pubsub = self._redis.pubsub()
            self._task = asyncio.create_task(self._listen_loop())

        if self._pubsub:
            try:
                await self._pubsub.subscribe(channel)
            except Exception as e:
                logger.debug("Subscribe %s: %s", channel, e)

    async def _listen_loop(self):
        while True:
            try:
                async for msg in self._pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    channel = msg.get("channel", "")
                    try:
                        payload = json.loads(msg.get("data", "{}"))
                    except json.JSONDecodeError:
                        continue
                    for cb in self._listeners.get(channel, []):
                        try:
                            await cb(payload)
                        except Exception as e:
                            logger.warning("Listener %s: %s", channel, e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception("Listen loop: %s", e)
                await asyncio.sleep(2)

    async def register_instance(self, info: dict):
        if not self._redis:
            return
        key = f"{self.cfg.channel_instance_prefix}{self.cfg.instance_id}"
        try:
            await self._redis.setex(
                key,
                self.cfg.instance_ttl_seconds,
                json.dumps({
                    **info,
                    "last_heartbeat": time.time(),
                }, default=str),
            )
        except Exception as e:
            logger.debug("Register instance: %s", e)

    async def list_instances(self) -> list[dict]:
        if not self._redis:
            return []
        try:
            keys = await self._redis.keys(
                f"{self.cfg.channel_instance_prefix}*"
            )
            result = []
            for k in keys:
                v = await self._redis.get(k)
                if v:
                    try:
                        result.append(json.loads(v))
                    except Exception:
                        pass
            return result
        except Exception:
            return []

    async def close(self):
        if self._task:
            self._task.cancel()
        if self._pubsub:
            try:
                await self._pubsub.close()
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
